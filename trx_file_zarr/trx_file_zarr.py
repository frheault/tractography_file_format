from copy import deepcopy
import logging
import os
import shutil

from dipy.io.stateful_tractogram import StatefulTractogram, Space
from dipy.io.utils import get_reference_info
from nibabel.affines import voxel_sizes
from nibabel.orientations import aff2axcodes
from nibabel.streamlines.array_sequence import ArraySequence
from nibabel.streamlines.tractogram import PerArraySequenceDict, PerArrayDict
import numpy as np
import zarr
from zarr.util import TreeViewer


def intersect_groups(group, indices):
    if np.issubdtype(type(indices), np.integer):
        indices = np.array([indices])

    index = np.argsort(indices)
    sorted_x = indices[index]
    sorted_index = np.searchsorted(sorted_x, group)
    yindex = np.take(index, sorted_index, mode="clip")
    mask = indices[yindex] != group

    return yindex[~mask]


def compute_lengths(offsets, nb_points):
    """ Compute lengths from offsets and header information """
    if len(offsets) > 1:
        lengths = np.ediff1d(offsets, to_end=nb_points-offsets[-1])
    elif len(offsets) == 1:
        lengths = np.array([nb_points])
    else:
        lengths = np.array([0])

    return lengths.astype(np.uint32)


def concatenate(trx_list):
    new_trx = TrxFile(init_as=trx_list[0])
    for trx in trx_list:
        new_trx.append(trx)

    return new_trx


def load(input_obj):
    """ """
    trx = TrxFile()
    if isinstance(input_obj, str):
        if os.path.isdir(input_obj):
            store = zarr.storage.DirectoryStore(input_obj)
        elif os.path.isfile(input_obj) and \
                os.path.splitext(input_obj)[1] in ['.zip', '.trx']:
            store = zarr.ZipStore(input_obj)
        else:
            raise ValueError('Invalid input path/filename.')
    else:
        store = input_obj

    trx._zcontainer = zarr.group(store=store, overwrite=False)
    trx.storage = store

    return trx


def save(trx, output_path):
    if os.path.splitext(output_path)[1] in ['.zip', '.trx']:
        if os.path.isfile(output_path):
            os.remove(output_path)
        store = zarr.ZipStore(output_path)
    elif os.path.splitext(output_path)[1] == '':
        if os.path.isdir(output_path):
            shutil.rmtree(output_path)
        store = zarr.storage.DirectoryStore(output_path)
    else:
        raise ValueError('Invalid output path/filename.')

    zarr.convenience.copy_store(trx.storage, store)
    if isinstance(store, zarr.storage.TempStore):
        store.rmdir()
    elif isinstance(store, zarr.storage.ZipStore):
        store.close()
    elif isinstance(store, zarr.storage.MemoryStore):
        store.clear()


def _check_same_keys(key_1, key_2):
    key_1 = list(key_1)
    key_2 = list(key_2)
    key_1.sort()
    key_2.sort()
    return key_1 == key_2


class TrxFile():
    """ Core class of the TrxFile """

    def __init__(self, init_as=None, reference=None,
                 store=None):
        """ Initialize an empty TrxFile, support preallocation """
        if init_as is not None:
            affine = init_as._zcontainer.attrs['VOXEL_TO_RASMM']
            dimensions = init_as._zcontainer.attrs['DIMENSIONS']
        elif reference is not None:
            affine, dimensions, _, _ = get_reference_info(reference)
        else:
            logging.debug('No reference provided, using blank space '
                          'attributes, please update them later.')
            affine = np.eye(4).astype(np.float32)
            dimensions = [1, 1, 1]

        if store is None:
            store = zarr.storage.TempStore()
        self._zcontainer = zarr.group(store=store, overwrite=True)
        self.voxel_to_rasmm = affine
        self.dimensions = dimensions
        self.nb_points = 0
        self.nb_streamlines = 0
        self._zstore = store

        if init_as:
            positions_dtype = init_as._zpos.dtype
        else:
            positions_dtype = np.float16
        self._zcontainer.create_dataset('positions', shape=(0, 3),
                                        chunks=(1000000, None),
                                        dtype=positions_dtype)

        self._zcontainer.create_dataset('offsets', shape=(0,),
                                        chunks=(100000,), dtype=np.uint64)

        self._zcontainer.create_group('data_per_point')
        self._zcontainer.create_group('data_per_streamline')
        self._zcontainer.create_group('data_per_group')
        self._zcontainer.create_group('groups')

        if init_as is None:
            return

        for dpp_key in init_as._zdpp.array_keys():
            empty_shape = list(init_as._zdpp[dpp_key].shape)
            empty_shape[0] = 0
            dtype = init_as._zdpp[dpp_key].dtype
            chunks = [1000000]
            for _ in range(len(empty_shape)-1):
                chunks.append(None)

            self._zdpp.create_dataset(dpp_key, shape=empty_shape,
                                      chunks=chunks, dtype=dtype)

        for dps_key in init_as._zdps.array_keys():
            empty_shape = list(init_as._zdps[dps_key].shape)
            empty_shape[0] = 0
            dtype = init_as._zdps[dps_key].dtype
            chunks = [100000]
            for _ in range(len(empty_shape)-1):
                chunks.append(None)

            self._zdps.create_dataset(dps_key, shape=empty_shape,
                                      chunks=chunks, dtype=dtype)

        for grp_key in init_as._zgrp.array_keys():
            empty_shape = list(init_as._zgrp[grp_key].shape)
            empty_shape[0] = 0
            dtype = init_as._zgrp[grp_key].dtype
            self._zgrp.create_dataset(grp_key, shape=empty_shape,
                                      chunks=(10000,), dtype=dtype)

        for grp_key in init_as._zdpg.group_keys():
            if len(init_as._zdpg[grp_key]):
                self._zdpg.create_group(grp_key)
            for dpg_key in init_as._zdpg[grp_key].array_keys():
                empty_shape = list(init_as._zdpg[grp_key][dpg_key].shape)
                empty_shape[0] = 0
                dtype = init_as._zdpg[grp_key][dpg_key].dtype
                self._zdpg[grp_key].create_dataset(dpg_key, shape=empty_shape,
                                                   chunks=None, dtype=dtype)

    def append(self, app_trx, delete_dpg=False, keep_first_dpg=True):
        """ Append TrxFile with strict metadata check """
        if not np.allclose(self.voxel_to_rasmm,
                           app_trx.voxel_to_rasmm) \
                or not np.array_equal(self.dimensions,
                                      app_trx.dimensions):
            raise ValueError('Mismatched space attributes between TrxFile.')

        if isinstance(self.storage, zarr.storage.ZipStore):
            raise ValueError('Cannot append to a Zip. Either unzip first, \n'
                             ' save to directory or init a new TrxFile (with '
                             'init_as) to append.')

        if delete_dpg and keep_first_dpg:
            raise ValueError('Cannot delete and keep data_per_group at the '
                             'same time.')

        if not self.is_empty() and not app_trx.is_empty() and \
            not _check_same_keys(self._zdpp.array_keys(),
                                 app_trx._zdpp.array_keys()):
            raise ValueError('data_per_point keys must fit to append.')
        if not self.is_empty() and not app_trx.is_empty() and \
            not _check_same_keys(self._zdps.array_keys(),
                                 app_trx._zdps.array_keys()):
            raise ValueError('data_per_streamline keys must fit to append.')
        if not (delete_dpg or keep_first_dpg) and \
                (len(self._zdpg) or len(self._zdpg)):
            raise ValueError('Choose a strategy for data_per_group: '
                             'delete_dpg or keep_first_dpg.')

        self._zpos.append(app_trx._zpos)
        self._zoff.append(np.array(app_trx._zoff) + self.nb_points)

        self.nb_points += app_trx.nb_points
        self.nb_streamlines += app_trx.nb_streamlines

        if app_trx.is_empty():
            return
        for dpp_key in self._zdpp.array_keys():
            self._zdpp[dpp_key].append(app_trx._zdpp[dpp_key])
        for dps_key in self._zdps.array_keys():
            self._zdps[dps_key].append(app_trx._zdps[dps_key])
        for grp_key in self._zgrp.array_keys():
            if grp_key in app_trx._zgrp:
                self._zgrp[grp_key].append(np.array(app_trx._zgrp[grp_key]) +
                                           self.nb_streamlines)

        if keep_first_dpg:
            for grp_key in self._zdpg.group_keys():
                for dpg_key in self._zdpg[grp_key].array_keys():
                    if grp_key in app_trx._zdpg and \
                            dpg_key in app_trx._zdpg[grp_key]:
                        self._zdpg[grp_key][dpg_key].append(
                            app_trx._zdpg[grp_key][dpg_key])

    def tree(self):
        self._zcontainer.tree()

    @staticmethod
    def from_sft(sft, cast_position=np.float16):
        """ Generate a valid TrxFile from a StatefulTractogram """
        if not np.issubdtype(cast_position, np.floating):
            logging.warning('Casting as {}, considering using a floating '
                            'point dtype.'.format(cast_position))

        trx = TrxFile()
        trx.voxel_to_rasmm = sft.affine.tolist()
        trx.dimensions = sft.dimensions
        trx.nb_streamlines = len(sft.streamlines._lengths)
        trx.nb_points = len(sft.streamlines._data)

        old_space = deepcopy(sft.space)
        old_origin = deepcopy(sft.origin)
        sft.to_rasmm()
        sft.to_center()

        del trx._zpos, trx._zoff
        trx._zcontainer.create_dataset('positions',
                                       data=sft.streamlines._data,
                                       chunks=(100000, None), dtype=np.float16)
        trx._zcontainer.create_dataset('offsets',
                                       data=sft.streamlines._offsets,
                                       chunks=(1000,), dtype=np.uint64)

        for dpp_key in sft.data_per_point.keys():
            trx._zdpp.create_dataset(dpp_key,
                                     data=sft.data_per_point[dpp_key]._data,
                                     chunks=(100000, None), dtype=np.float32)
        for dps_key in sft.data_per_streamline.keys():
            trx._zdps.create_dataset(dps_key,
                                     data=sft.data_per_streamline[dps_key],
                                     chunks=(10000, None), dtype=np.float32)
        sft.to_space(old_space)
        sft.to_origin(old_origin)

        return trx

    def to_sft(self):
        """ Convert a TrxFile to a valid StatefulTractogram """
        affine = self.voxel_to_rasmm
        dimensions = self.dimensions
        vox_sizes = np.array(voxel_sizes(affine), dtype=np.float32)
        vox_order = ''.join(aff2axcodes(affine))
        space_attributes = (affine, dimensions, vox_sizes, vox_order)

        sft = StatefulTractogram(
            self.streamlines, space_attributes, Space.RASMM,
            data_per_point=self.consolidate_data_per_point(),
            data_per_streamline=self.consolidate_data_per_streamline())

        return sft

    def __deepcopy__(self):
        return self.deepcopy()

    def deepcopy(self, store=None):
        if store is None:
            store = zarr.storage.TempStore()

        zarr.convenience.copy_store(self.storage, store)
        new_trx = load(store)

        return new_trx

    def is_empty(self):
        self.prune_metadata()

        is_empty = True
        if len(list(self._zdpp.array_keys())) or \
                len(list(self._zdps.array_keys())) or \
                len(list(self._zgrp.array_keys())) or \
                len(list(self._zdpg.group_keys())) or \
                len(list(self._zcontainer['positions'])) or \
                len(list(self._zcontainer['offsets'])):
            is_empty = False
        return is_empty

    def __getitem__(self, key):
        """ Slice all data in a consistent way """
        indices = np.arange(self.nb_streamlines)[key]
        return self.select(indices)

    def get_group(self, key):
        """ Select the items of a specific groups from the TrxFile """
        return self.select(self._zgrp[key])

    def select(self, indices, keep_group=True):
        """ Get a subset of items, always points to the same memmaps """
        indices = np.array(indices, np.uint32)
        if len(indices) and (np.max(indices) > self.nb_streamlines - 1 or
                             np.min(indices) < 0):
            raise ValueError('Invalid indices.')

        new_trx = TrxFile(init_as=self)
        if len(indices) == 0:
            new_trx.prune_metadata()
            return new_trx

        tmp_streamlines = self.streamlines
        new_trx._zpos.append(tmp_streamlines[indices].get_data())
        new_offsets = np.cumsum(tmp_streamlines[indices]._lengths[:-1])
        new_trx._zoff.append(np.concatenate(([0], new_offsets)))

        for dpp_key in self._zdpp.array_keys():
            tmp_dpp = ArraySequence()
            tmp_dpp._data = np.array(self._zdpp[dpp_key])
            tmp_dpp._offsets = tmp_streamlines._offsets
            tmp_dpp._lengths = tmp_streamlines._lengths

            new_trx._zdpp[dpp_key].append(tmp_dpp[indices].get_data())

        for dps_key in self._zdps.array_keys():
            new_trx._zdps[dps_key].append(
                np.array(self._zdps[dps_key])[indices])

        if keep_group:
            for grp_key in self._zgrp.array_keys():
                new_group = intersect_groups(self._zgrp[grp_key], indices)

                if len(new_group):
                    new_trx._zgrp[grp_key].append(new_group)
                else:
                    del new_trx._zcontainer['groups'][grp_key]

        for grp_key in self._zdpg.group_keys():
            if grp_key in new_trx._zgrp:
                for dpg_key in self._zdpg[grp_key].array_keys():
                    new_trx._zdpg[grp_key][dpg_key].append(
                        self._zdpg[grp_key][dpg_key])

        new_trx.nb_streamlines = len(new_trx._zoff)
        new_trx.nb_points = len(new_trx._zpos)
        new_trx.prune_metadata()

        return new_trx

    def prune_metadata(self, force=False):
        """ Prune empty arrays of the metadata """
        for dpp_key in self._zdpp.array_keys():
            if self._zdpp[dpp_key].shape[0] == 0 or force:
                del self._zcontainer['data_per_point'][dpp_key]

        for dps_key in self._zdps.array_keys():
            if self._zdps[dps_key].shape[0] == 0 or force:
                del self._zcontainer['data_per_streamline'][dps_key]

        for grp_key in self._zgrp.array_keys():
            if self._zgrp[grp_key].shape[0] == 0 or force:
                del self._zcontainer['groups'][grp_key]

        for grp_key in self._zdpg.group_keys():
            if grp_key not in self._zgrp:
                del self._zcontainer['data_per_group'][grp_key]
                continue
            for dpg_key in self._zdpg[grp_key].array_keys():
                if self._zdpg[grp_key][dpg_key].shape[0] == 0 or force:
                    del self._zcontainer['data_per_group'][grp_key][dpg_key]

    def consolidate_data_per_streamline(self):
        """ Convert the zarr representation of data_per_streamline to
        memory PerArrayDict (nibabel)"""
        dps_arr_dict = PerArrayDict()
        for dps_key in self._zdps.array_keys():
            dps_arr_dict[dps_key] = self._zdps[dps_key]

        return dps_arr_dict

    def consolidate_data_per_point(self):
        """ Convert the zarr representation of data_per_point to
        memory PerArraySequenceDict (nibabel)"""
        dpp_arr_seq_dict = PerArraySequenceDict()
        for dpp_key in self._zdpp.array_keys():
            arr_seq = ArraySequence()
            arr_seq._data = self._zdpp[dpp_key]
            arr_seq._offsets = self._zoff
            arr_seq._lengths = compute_lengths(arr_seq._offsets,
                                               self.nb_points)
            if arr_seq._data.ndim == 1:
                arr_seq._data = np.expand_dims(arr_seq._data, axis=-1)
            dpp_arr_seq_dict[dpp_key] = arr_seq

        return dpp_arr_seq_dict

    @ property
    def consolidate_groups(self):
        """ Convert the zarr representation of groups to memory dict of
        np.ndarray """
        group_dict = {}
        for grp_key in self._zcontainer['groups'].array_keys():
            group_dict[grp_key] = np.array(self._zcontainer['groups'][grp_key])
        return group_dict

    @ property
    def streamlines(self):
        """ """
        streamlines = ArraySequence()
        streamlines._data = np.array(self._zpos)
        streamlines._offsets = np.array(self._zoff)
        streamlines._lengths = compute_lengths(streamlines._offsets,
                                               self.nb_points)
        return streamlines

    @ property
    def voxel_to_rasmm(self):
        """ """
        return np.array(self._zcontainer.attrs['VOXEL_TO_RASMM'],
                        dtype=np.float32)

    @ voxel_to_rasmm.setter
    def voxel_to_rasmm(self, val):
        if isinstance(val, np.ndarray):
            val = val.astype(np.float32).tolist()
        self._zcontainer.attrs['VOXEL_TO_RASMM'] = val

    @ property
    def dimensions(self):
        """ """
        return np.array(self._zcontainer.attrs['DIMENSIONS'], dtype=np.uint16)

    @ dimensions.setter
    def dimensions(self, val):
        if isinstance(val, np.ndarray):
            val = val.astype(np.uint16).tolist()
        self._zcontainer.attrs['DIMENSIONS'] = val

    @ property
    def nb_streamlines(self):
        """ """
        return self._zcontainer.attrs['NB_STREAMLINES']

    @ nb_streamlines.setter
    def nb_streamlines(self, val):
        self._zcontainer.attrs['NB_STREAMLINES'] = int(val)

    @ property
    def nb_points(self):
        """ """
        return self._zcontainer.attrs['NB_POINTS']

    @ nb_points.setter
    def nb_points(self, val):
        self._zcontainer.attrs['NB_POINTS'] = int(val)

    @ property
    def _zpos(self):
        """ """
        return self._zcontainer['positions']

    @ property
    def _zoff(self):
        """ """
        return self._zcontainer['offsets']

    @ property
    def _zdpp(self):
        """ """
        return self._zcontainer['data_per_point']

    @ property
    def _zdps(self):
        """ """
        return self._zcontainer['data_per_streamline']

    @ property
    def _zdpg(self):
        """ """
        return self._zcontainer['data_per_group']

    @ property
    def _zgrp(self):
        """ """
        return self._zcontainer['groups']

    def __str__(self):
        """ Generate the string for printing """
        affine = np.array(self.voxel_to_rasmm, dtype=np.float32)
        dimensions = np.array(self.dimensions, dtype=np.uint16)
        vox_sizes = np.array(voxel_sizes(affine), dtype=np.float32)
        vox_order = ''.join(aff2axcodes(affine))

        text = 'VOXEL_TO_RASMM: \n{}'.format(
            np.array2string(affine,
                            formatter={'float_kind': lambda x: "%.6f" % x}))
        text += '\nDIMENSIONS: {}'.format(
            np.array2string(dimensions))
        text += '\nVOX_SIZES: {}'.format(
            np.array2string(vox_sizes,
                            formatter={'float_kind': lambda x: "%.2f" % x}))
        text += '\nVOX_ORDER: {}'.format(vox_order)

        text += '\nNB_STREAMLINES: {}'.format(self.nb_streamlines)
        text += '\nNB_POINTS: {}'.format(self.nb_points)

        text += '\n'+TreeViewer(self._zcontainer).__unicode__()

        return text

    @ property
    def storage(self):
        return self._zstore

    @ storage.setter
    def storage(self, val):
        if self._zstore is not None:
            self.close()
        self._zstore = val

    def close(self):
        self.__del__()

    def __del__(self):
        if isinstance(self._zstore, zarr.storage.TempStore):
            self._zstore.rmdir()
        elif isinstance(self._zstore, zarr.storage.ZipStore):
            self._zstore.close()
        elif isinstance(self._zstore, zarr.storage.MemoryStore):
            self._zstore.clear()
        else:
            logging.debug('Cannot close an user defined directory.')
