#!/usr/bin/env python
"""Phonon band structure of AlAs."""
from __future__ import division, print_function

import sys
import ast
import os
import numpy as np
import abipy.abilab as abilab
import abipy.data as abidata

from abipy.core.structure import Structure
import abipy.abilab
from pymatgen.io.gwwrapper.helpers import refine_structure
from pymatgen.io.abinitio.pseudos import PseudoTable


def unique_rows(a):
    a = np.ascontiguousarray(a)
    unique_a = np.unique(a.view([('', a.dtype)]*a.shape[1]))
    return unique_a.view(a.dtype).reshape((unique_a.shape[0], a.shape[1]))


def to_vec(var_dict, var):
    """
    turn the entry var in the dictionary var_dict into an  vector length 3
    """
    tmp = var_dict[var]
    tmp = 3*[tmp] if not isinstance(tmp, (list, tuple)) else tmp
    var_dict[var] = tmp


def to_vecs(var_dict):
    """
    turn the items in list into vectors if they are present
    """
    vec_list = ['ngkpt', 'acell']
    for var in vec_list:
        if var in var_dict.keys():
            to_vec(var_dict, var)


def scf_ph_inputs(structure, options):
    """
    This function constructs the input files for the phonon calculation: 
    GS input + the input files for the phonon calculation.
    """

    abi_pseudo = os.environ['ABINIT_PS_EXT']
    abi_pseudo_dir = os.environ['ABINIT_PS']
    pseudos = []
    for element in structure.composition.element_composition:
        pseudo = os.path.join(abi_pseudo_dir, str(element) + abi_pseudo)
        pseudos.append(pseudo)
    pseudos = PseudoTable(pseudos)

    #print('bounds:\n', structure.calc_kptbounds)
    #print('ngkpt:\n', structure.calc_ngkpt(4))
    print('ks:\n', structure.calc_ksampling(4)) # try to get the qpoints from this ...

    qptbounds = structure.calc_kptbounds()
    qptbounds = np.reshape(qptbounds, (-1, 3))

    # List of q-points for the phonon calculation.
    qpoints = [
             0.00000000E+00,  0.00000000E+00,  0.00000000E+00, 
             2.50000000E-01,  0.00000000E+00,  0.00000000E+00,
             2.50000000E-01,  0.00000000E+00,  2.50000000E+00,
             5.00000000E-01,  0.00000000E+00,  0.00000000E+00,
             2.50000000E-01,  2.50000000E-01,  0.00000000E+00,
             5.00000000E-01,  2.50000000E-01,  0.00000000E+00,
            -2.50000000E-01,  2.50000000E-01,  0.00000000E+00,
             5.00000000E-01,  5.00000000E-01,  0.00000000E+00,
             0.00000000E+00,  0.00000000E+00,  2.50000000E-01,
            -2.50000000E-01,  5.00000000E-01,  2.50000000E-01,
            ]
    qpoints2 = [
             0.00000000E+00,  0.00000000E+00,  0.00000000E+00,
             5.00000000E-01,  0.00000000E+00,  0.00000000E+00,
             0.00000000E-01,  5.00000000E-01,  0.00000000E+00,
             0.00000000E+00,  0.00000000E+00,  5.00000000E-01,
             5.00000000E-01,  5.00000000E-01,  0.00000000E+00,
             0.00000000E+00,  5.00000000E-01,  5.00000000E-01,
             5.00000000E-01,  0.00000000E+00,  5.00000000E-01,
             5.00000000E-01,  5.00000000E-01,  5.00000000E-01,
            ]

    qpoints = np.reshape(qpoints, (-1, 3))
    qpoints = unique_rows(np.concatenate((qpoints, qptbounds), axis=0))

    if os.path.isfile('qpoints'):
        f = open('qpoints', 'r')
        qpoints = np.reshape(ast.literal_eval(f.read()), (-1, 3))
        f.close()

    # Global variables used both for the GS and the DFPT run.
    global_vars = dict(
        istwfk=100000*1,
        ecut=16.0,
        ngkpt=[4, 4, 4],
        shiftk=[0, 0, 0],
        paral_kgb=0,
        nstep=200)

    global_vars.update(options)

    to_vecs(global_vars)

    inp = abilab.AbiInput(pseudos=pseudos, ndtset=1+len(qpoints))

    inp.set_structure(structure)
    inp.set_variables(**global_vars)

    inp[1].set_variables(tolwfr=1.0e-18, prtden=1, paral_kgb=1)

    for i, qpt in enumerate(qpoints):
        # Response-function calculation for phonons.
        inp[i+2].set_variables(
            tolvrs=1.0e-10,
            kptopt=3,
            iscf=5,
            rfphon=1,        # Will consider phonon-type perturbation
            nqpt=1,          # One wavevector is to be considered
            qpt=qpt,         # This wavevector is q=0 (Gamma)
            )

            #rfatpol   1 1   # Only the first atom is displaced
            #rfdir   1 0 0   # Along the first reduced coordinate axis
            #kptopt   2      # Automatic generation of k points, taking

    # Split input into gs_inp and ph_inputs
    return inp.split_datasets()


def run_annaddb(flow, structure):

    #structure = flow[0][0].
    manager = abilab.TaskManager.from_user_config()

    # We should have a DDB files with IFC(q) in work.outdir
    ddb_files = []
    for work in flow[1:]:
        ddbs = work.outdir.list_filepaths(wildcard="*DDB")
        assert len(ddbs) == 1
        ddb_files.append(ddbs[0])

    # TODO: Check automatic restart
    assert all(work.finalized for work in flow)
    assert flow.all_ok

    # Merge the DDB files
    out_ddb = flow.outdir.path_in("flow_DDB")
    ddb_path = abilab.Mrgddb().merge(ddb_files, out_ddb=out_ddb, description="DDB generated by %s" % __file__,
                                     cwd=flow.outdir.path)
    assert ddb_path == out_ddb

    # Build new workflow with Anaddb tasks.
    # Construct a manager with mpi_ncpus==1 since  anaddb do not support mpi_ncpus > 1 (except in elphon)
    shell_manager = manager.to_shell_manager()
    awork = abilab.Workflow(manager=shell_manager)

    # modes
    anaddb_input = abilab.AnaddbInput.modes(structure)
    atask = abilab.AnaddbTask(anaddb_input, ddb_node=ddb_path, manager=shell_manager)
    awork.register(atask)

    # Thermodynamics
    anaddb_input = abilab.AnaddbInput.thermo(structure, ngqpt=(40, 40, 40), nqsmall=20)
    atask = abilab.AnaddbTask(anaddb_input, ddb_node=ddb_path, manager=shell_manager)
    awork.register(atask)

    # Phonons bands and DOS with gaussian method
    anaddb_input = abilab.AnaddbInput.phbands_and_dos(
        structure, ngqpt=(4, 4, 4), ndivsm=5, nqsmall=10, dos_method="gaussian: 0.001 eV")
    atask = abilab.AnaddbTask(anaddb_input, ddb_node=ddb_path, manager=shell_manager)
    awork.register(atask)

    # Phonons bands and DOS with tetrahedron method
    anaddb_input = abilab.AnaddbInput.phbands_and_dos(
        structure, ngqpt=(4, 4, 4), ndivsm=5, nqsmall=10, dos_method="tetra")
    atask = abilab.AnaddbTask(anaddb_input, ddb_node=ddb_path, manager=shell_manager)
    awork.register(atask)

    flow.register_work(awork)
    flow.allocate()
    flow.build()

    for i, atask in enumerate(awork):
        print("about to run anaddb task: %d" % i)
        atask.start_and_wait()
        #assert atask.status == atask.S_DONE
        atask.check_status()
        #assert atask.status == atask.S_OK

        # TODO: output files are not produced in outdir
        #assert len(atask.outdir.list_filepaths(wildcard="*PHBST.nc")) == 1
        #assert len(atask.outdir.list_filepaths(wildcard="*PHDOS.nc")) == 1


def build_flow(structure, workdir, options):
    """
    Create an `AbinitFlow` for phonon calculations:

        1) One workflow for the GS run.

        2) nqpt workflows for phonon calculations. Each workflow contains 
           nirred tasks where nirred is the number of irreducible phonon perturbations
           for that particular q-poin t.
    """

    # Instantiate the TaskManager.
    manager = abilab.TaskManager.from_user_config()

    all_inps = scf_ph_inputs(structure, options)
    scf_input, ph_inputs = all_inps[0], all_inps[1:]
    if len(ph_inputs) == 1:
        return abilab.phonon_flow(workdir, manager, scf_input, ph_inputs, with_nscf=False, with_ddk=True, with_dde=True)
    else:
        return abilab.phonon_flow(workdir, manager, scf_input, ph_inputs, with_nscf=True, with_ddk=False, with_dde=False)


class NotReady(Exception):
    """
    previous flow is not complete
    """


#@abilab.flow_main
def main():

    cifs = [f for f in os.listdir('.') if f.endswith('cif')]
    convtests = {'ecut': [37], 'ngkpt': [8], 'acell': [1.0]}

    for cif in cifs:
        structure = Structure.from_file(cif)
        print(type(structure))
        structure = structure.get_sorted_structure_z()
        print(structure)
        structure.item = cif
        for convtest in convtests:
            for value in convtests[convtest]:

                workdir = '%s_%s_%s' % (structure.item, str(convtest), str(value))

                try:
                    flow = abilab.AbinitFlow.pickle_load(workdir)
                    if not flow.all_ok:
                        raise NotReady
                    run_annaddb(flow=flow, structure=structure)
                except NotReady:
                    pass
                except (ValueError, IOError):
                    options = {convtest: value}
                    flow = build_flow(structure=structure, workdir=workdir, options=options)
                    flow.build_and_pickle_dump()


if __name__ == "__main__":
    main()
