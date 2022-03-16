# -*- coding: utf-8 -*-
"""Data utilities for U-net training and prediction.
"""
import glob
import logging
import os
import re
import sys
import warnings
from datetime import date
from itertools import chain, product
from pathlib import Path

import h5py as h5
import imageio
import numpy as np
import torch
import yaml
from fastai.vision import Image, crop_pad, pil2tensor
from skimage import exposure, img_as_float, img_as_ubyte, io
from skimage.measure import block_reduce
from tqdm import tqdm

from . import config as cfg

warnings.filterwarnings("ignore", category=UserWarning)


class SettingsData:
    """Class to store settings from a YAML settings file.

    Args:
        settings_path (pathlib.Path): Path to the YAML file containing user settings.
    """
    def __init__(self, settings_path):
        logging.info(f"Loading settings from {settings_path}")
        if settings_path.exists():
            self.settings_path = settings_path
            with open(settings_path, 'r') as stream:
                self.settings_dict = yaml.safe_load(stream)
        else:
            logging.error("Couldn't find settings file... Exiting!")
            sys.exit(1)

        # Set the data as attributes
        for k, v in self.settings_dict.items():
            setattr(self, k, v)
                    

class DataSlicerBase:
    """Base class for classes that convert 3d data volumes into 2d image slices on disk.
    Slicing is carried in all of the xy (z), xz (y) and yz (x) planes.

    Args:
        settings (SettingsData): An initialised SettingsData object.
    """

    def __init__(self, settings):
        self.input_data_chunking = None
        self.st_dev_factor = settings.st_dev_factor
        self.downsample = settings.downsample
        if self.downsample:
            self.data_vol = self.downsample_data(self.data_vol)
        self.data_vol_shape = self.data_vol.shape
        logging.info("Calculating mean of data...")
        self.data_mean = np.nanmean(self.data_vol)
        logging.info(f"Mean value: {self.data_mean}")
        if settings.clip_data:
            self.data_vol = self.clip_to_uint8(self.data_vol)
        if np.isnan(self.data_vol).any():
            logging.info(f"Replacing NaN values.")
            self.data_vol = np.nan_to_num(self.data_vol, copy=False)
        

    def downsample_data(self, data, factor=2):
        logging.info(f"Downsampling data by a factor of {factor}.")
        return block_reduce(data, block_size=(factor, factor, factor), func=np.nanmean)

    def get_numpy_from_path(self, path, internal_path="/data"):
        """Helper function that returns numpy array according to file extension.

        Args:
            path (pathlib.Path): The path to the data file. 
            internal_path (str, optional): Internal path within HDF5 file. Defaults to "/data".

        Returns:
            numpy.ndarray: Numpy array from the file given in the path.
        """
        if path.suffix in cfg.TIFF_SUFFIXES:
            return self.numpy_from_tiff(path)
        elif path.suffix in cfg.HDF5_SUFFIXES:
            nexus = path.suffix == ".nxs"
            return self.numpy_from_hdf5(path,
                                        hdf5_path=internal_path,
                                        nexus=nexus)
    
    def numpy_from_tiff(self, path):
        """Returns a numpy array when given a path to an multipage TIFF file.

        Args:
            path(pathlib.Path): The path to the TIFF file.

        Returns:
            numpy.array: A numpy array object for the data stored in the TIFF file.
        """
        
        return imageio.volread(path)

    def numpy_from_hdf5(self, path, hdf5_path='/data', nexus=False):
        """Returns a numpy array when given a path to an HDF5 file.

        The data is assumed to be found in '/data' in the file.

        Args:
            path(pathlib.Path): The path to the HDF5 file.
            hdf5_path (str): The internal HDF5 path to the data.

        Returns:
            numpy.array: A numpy array object for the data stored in the HDF5 file.
        """
        
        data_handle = h5.File(path, 'r')
        if nexus:
            try:
                dataset = data_handle['processed/result/data']
            except KeyError:
                logging.error("NXS file: Couldn't find data at 'processed/result/data' trying another path.")
                try:
                    dataset = data_handle['entry/final_result_tomo/data']
                except KeyError:
                    logging.error("NXS file: Could not find entry at entry/final_result_tomo/data, exiting!")
                    sys.exit(1)
        else:
            dataset = data_handle[hdf5_path]
        self.input_data_chunking = dataset.chunks
        return dataset[()]

    def clip_to_uint8(self, data):
        """Clips data to a certain number of st_devs of the mean and reduces
        bit depth to uint8.

        Args:
            data(np.array): The data to be processed.

        Returns:
            np.array: A unit8 data array.
        """
        logging.info("Clipping data and converting to uint8.")
        logging.info(f"Calculating standard deviation.")
        data_st_dev = np.nanstd(data)
        logging.info(f"Std dev: {data_st_dev}. Calculating stats.")
        # diff_mat = np.ravel(data - self.data_mean)
        # data_st_dev = np.sqrt(np.dot(diff_mat, diff_mat)/data.size)
        num_vox = data.size
        lower_bound = self.data_mean - (data_st_dev * self.st_dev_factor)
        upper_bound = self.data_mean + (data_st_dev * self.st_dev_factor)
        with np.errstate(invalid='ignore'):
            gt_ub = (data > upper_bound).sum()
            lt_lb = (data < lower_bound).sum()
        logging.info(f"Lower bound: {lower_bound}, upper bound: {upper_bound}")
        logging.info(
            f"Number of voxels above upper bound to be clipped {gt_ub} - percentage {gt_ub/num_vox * 100:.3f}%")
        logging.info(
            f"Number of voxels below lower bound to be clipped {lt_lb} - percentage {lt_lb/num_vox * 100:.3f}%")
        if np.isnan(data).any():
            logging.info(f"Replacing NaN values.")
            data = np.nan_to_num(data, copy=False, nan=self.data_mean)
        logging.info("Rescaling intensities.")
        if np.issubdtype(data.dtype, np.integer):
            logging.info("Data is already in integer dtype, converting to float for rescaling.")
            data = data.astype(np.float)
        data = np.clip(data, lower_bound, upper_bound, out=data)
        data = np.subtract(data, lower_bound, out=data)
        data = np.divide(data, (upper_bound - lower_bound), out=data)
        #data = (data - lower_bound) / (upper_bound - lower_bound)
        data = np.clip(data, 0.0, 1.0, out=data)
        # data = exposure.rescale_intensity(data, in_range=(lower_bound, upper_bound))
        logging.info("Converting to uint8.")
        data = np.multiply(data, 255, out=data)
        return data.astype(np.uint8)

    def get_axis_index_pairs(self, vol_shape):
        """Gets all combinations of axis and image slice index that are found
        in a 3d volume.

        Args:
            vol_shape (tuple): 3d volume shape (z, y, x)

        Returns:
            itertools.chain: An iterable containing all combinations of axis
            and image index that are found in the volume.
        """
        return chain(
            product('z', range(vol_shape[0])),
            product('y', range(vol_shape[1])),
            product('x', range(vol_shape[2]))
        )

    def axis_index_to_slice(self, vol, axis, index):
        """Converts an axis and image slice index for a 3d volume into a 2d 
        data array (slice). 

        Args:
            vol (3d array): The data volume to be sliced.
            axis (str): One of 'z', 'y' and 'x'.
            index (int): An image slice index found in that axis. 

        Returns:
            2d array: A 2d image slice corresponding to the axis and index.
        """
        if axis == 'z':
            return vol[index, :, :]
        if axis == 'y':
            return vol[:, index, :]
        if axis == 'x':
            return vol[:, :, index]

    def get_num_of_ims(self, vol_shape):
        """Calculates the total number of images that will be created when slicing
        an image volume in the z, y and x planes.

        Args:
            vol_shape (tuple): 3d volume shape (z, y, x).

        Returns:
            int: Total number of images that will be created when the volume is
            sliced. 
        """
        return sum(vol_shape)



class TrainingDataSlicer(DataSlicerBase):
    """Class that converts 3d data volumes into 2d image slices on disk for
    model training.
    Slicing is carried in all of the xy (z), xz (y) and yz (x) planes.

    Args:
        settings (SettingsData): An initialised SettingsData object.
    """

    def __init__(self, settings, data_vol_path, label_vol_path):
        self.data_vol_path = Path(data_vol_path)
        self.data_vol = self.get_numpy_from_path(self.data_vol_path,
        										 internal_path=settings.train_data_hdf5_path)
       
        super().__init__(settings)
        self.multilabel = False
        self.data_im_out_dir = None
        self.seg_im_out_dir = None
        label_vol_path = Path(label_vol_path)
        self.seg_vol = self.get_numpy_from_path(label_vol_path,
                                         internal_path=settings.seg_hdf5_path)
        seg_classes = np.unique(self.seg_vol)
        self.num_seg_classes = len(seg_classes)
        if self.num_seg_classes > 2:
            self.multilabel = True
        logging.info("Number of classes in segmentation dataset:"
                     f" {self.num_seg_classes}")
        logging.info(f"These classes are: {seg_classes}")
        if seg_classes[0] != 0:
            logging.info("Fixing label classes.")
            self.fix_label_classes(seg_classes)
        self.codes = [f"label_val_{i}" for i in seg_classes]

    def fix_label_classes(self, seg_classes):
        """Changes the data values of classes in a segmented volume so that
        they start from zero.

        Args:
            seg_classes(list): An ascending list of the labels in the volume.
        """
        for idx, current in enumerate(seg_classes):
            self.seg_vol[self.seg_vol == current] = idx

    def output_data_slices(self, data_dir, prefix):
        """Wrapper method to intitiate slicing data volume to disk.

        Args:
            data_dir (pathlib.Path): The path to the directory where images will be saved.
        """
        self.data_im_out_dir = data_dir
        logging.info(
            'Slicing data volume and saving slices to disk')
        os.makedirs(data_dir, exist_ok=True)
        self.output_slices_to_disk(self.data_vol, data_dir, prefix)

    def output_label_slices(self, data_dir, prefix):
        """Wrapper method to intitiate slicing label volume to disk.

        Args:
            data_dir (pathlib.Path): The path to the directory where images will be saved.
        """
        self.seg_im_out_dir = data_dir
        logging.info(
            'Slicing label volume and saving slices to disk')
        os.makedirs(data_dir, exist_ok=True)
        self.output_slices_to_disk(
            self.seg_vol, data_dir, prefix, label=True)

    def output_slices_to_disk(self, data_arr, output_path, name_prefix, label=False):
        """Coordinates the slicing of an image volume in the three orthogonal
        planes to images on disk. 
        
        Args:
            data_arr (array): The data volume to be sliced.
            output_path (pathlib.Path): A Path object to the output directory.
            label (bool): Whether this is a label volume.
        """
        shape_tup = data_arr.shape
        ax_idx_pairs = self.get_axis_index_pairs(shape_tup)
        num_ims = self.get_num_of_ims(shape_tup)
        for axis, index in tqdm(ax_idx_pairs, total=num_ims):
            out_path = output_path/f"{name_prefix}_{axis}_stack_{index}"
            self.output_im(self.axis_index_to_slice(data_arr, axis, index),
                           out_path, label)

    def output_im(self, data, path, label=False):
        """Converts a slice of data into an image on disk.
    
        Args:
            data (numpy.array): The data slice to be converted.
            path (str): The path of the image file including the filename prefix.
            label (bool): Whether to convert values >1 to 1 for binary segmentation.
        """
        if label:
            if data.dtype != np.uint8:
                data = img_as_ubyte(data)
            if not self.multilabel:
                data[data > 1] = 1
        io.imsave(f'{path}.png', data)

    def delete_data_im_slices(self):
        """Deletes image slices in the data image output directory. Leaves the
        directory in place since it contains model training history.
        """
        if self.data_im_out_dir:
            data_ims = glob.glob(f"{str(self.data_im_out_dir) + '/*.png'}")
            logging.info(f"Deleting {len(data_ims)} image slices")
            for fn in data_ims:
                os.remove(fn)

    def delete_label_im_slices(self):
        """Deletes label image slices in the segmented image output directory.
        Also deletes the directory itself.
        """
        if self.seg_im_out_dir:
            seg_ims = glob.glob(f"{str(self.seg_im_out_dir) + '/*.png'}")
            logging.info(f"Deleting {len(seg_ims)} segmentation slices")
            for fn in seg_ims:
                os.remove(fn)
            logging.info(f"Deleting the empty segmentation image directory")
            os.rmdir(self.seg_im_out_dir)

    def clean_up_slices(self):
        """Wrapper function that cleans up data and label image slices.
        """
        self.delete_data_im_slices()
        self.delete_label_im_slices()


class PredictionDataSlicer(DataSlicerBase):
    """Class that converts 3d data volumes into 2d image slices for
    segmentation prediction and that combines the slices back into volumes after
    prediction. 

    1. Slicing is carried in the xy (z), xz (y) and yz (x) planes. 2. The data
    volume is rotated by 90 degrees. Steps 1 and 2 are then repeated untill
    4 rotations have been sliced.

    The class also has methods to combine the image slices in to 3d volumes and
    also to combine these volumes and perform consensus thresholding.

    Args:
        settings (SettingsData): An initialised SettingsData object.
        predictor (Unet2dPredictor): A Unet2dPredictor object with a trained
        2d U-net as an attribute.
    """

    def __init__(self, settings, predictor, data_vol_path):
        self.data_vol_path = Path(data_vol_path)
        self.data_vol =  self.get_numpy_from_path(self.data_vol_path,
        										  internal_path=settings.predict_data_hdf5_path)
        self.check_data_dims(self.data_vol.shape)
        super().__init__(settings)
        self.consensus_vals = map(int, settings.consensus_vals)
        self.predictor = predictor
        self.delete_vols = settings.del_vols # Whether to clean up predicted vols

    def check_data_dims(self, data_vol_shape):
        """Terminates program if one or more data dimensions is not even.

        Args:
            data_vol_shape (tuple): The shape of the data.
        """
        odd_dims = [x%2 != 0 for x in data_vol_shape]
        if any(odd_dims):
            logging.error(f"One or more data dimensions is not even: {data_vol_shape}. "
                            "Cannot currently predict odd-sized shapes, please change dimensions and try again.")
            sys.exit(1)

    def setup_folder_stucture(self, root_path):
        """Sets up a folder structure to store the predicted images.
    
        Args:
            root_path (Path): The top level directory for data output.
        """
        vol_dir= root_path/f'{date.today()}_predicted_volumes'
        non_rotated = vol_dir/f'{date.today()}_non_rotated_volumes'
        rot_90_seg = vol_dir/f'{date.today()}_rot_90_volumes'
        rot_180_seg = vol_dir/f'{date.today()}_rot_180_volumes'
        rot_270_seg = vol_dir/f'{date.today()}_rot_270_volumes'

        self.dir_list = [
            ('non_rotated', non_rotated),
            ('rot_90_seg', rot_90_seg),
            ('rot_180_seg', rot_180_seg),
            ('rot_270_seg', rot_270_seg)
        ]
        for _, dir_path in self.dir_list:
            os.makedirs(dir_path, exist_ok=True)

    def combine_slices_to_vol(self, folder_path):
        """Combines the orthogonally sliced png images in a folder to HDF5 
        volumes. One volume for each direction. These are then saved with a
        common orientation. The images slices are then deleted.

        Args:
            folder_path (pathlib.Path): Path to a folder containing images that
            were sliced in the three orthogonal planes. 

        Returns:
            list of pathlib.Path: Paths to the created volumes.
        """
        output_path_list = []
        file_list = folder_path.ls()
        axis_list = ['z', 'y', 'x']
        number_regex = re.compile(r'\_(\d+)\.png')
        for axis in axis_list:
            # Generate list of files for that axis
            axis_files = [x for x in file_list if re.search(
                f'\_({axis})\_', str(x))]
            logging.info(f'Axis {axis}: {len(axis_files)} files found, creating' \
                ' volume')
            # Load in the first image to get dimensions
            first_im = io.imread(axis_files[0])
            shape_tuple = first_im.shape
            z_dim = len(axis_files)
            y_dim, x_dim = shape_tuple
            data_vol = np.empty([z_dim, y_dim, x_dim], dtype=np.uint8)
            for filename in axis_files:
                m = number_regex.search(str(filename))
                index = int(m.group(1))
                im_data = io.imread(filename)
                data_vol[index, :, :] = im_data
            if axis == 'y':
                data_vol = np.swapaxes(data_vol, 0, 1)
            if axis == 'x':
                data_vol = np.swapaxes(data_vol, 0, 2)
                data_vol = np.swapaxes(data_vol, 0, 1)
            output_path = folder_path/f'{axis}_axis_seg_combined.h5'
            output_path_list.append(output_path)
            logging.info(f'Outputting {axis} axis volume to {output_path}')
            with h5.File(output_path, 'w') as f:
                f['/data'] = data_vol
            # Delete the images
            logging.info(f"Deleting {len(axis_files)} image files for axis {axis}")
            for filename in axis_files:
                os.remove(filename)
        return output_path_list

    def combine_vols(self, output_path_list, k, prefix, final=False):
        """Sums volumes to give a combination of binary segmentations and saves to disk.

        Args:
            output_path_list (list of pathlib.Path): Paths to the volumes to be combined.
            k (int): Number of 90 degree rotations that these image volumes
            have been transformed by before slicing. 
            prefix (str): A filename prefix to give the final volume.
            final (bool, optional): Set to True if this is the final combination
            of the volumes that were created from each of the 90 degree rotations.
            Defaults to False.

        Returns:
            pathlib.Path: A file path to the combined HDF5 volume that was saved.
        """
        num_vols = len(output_path_list)
        combined = self.numpy_from_hdf5(output_path_list[0])
        for subsequent in output_path_list[1:]:
            combined += self.numpy_from_hdf5(subsequent)
        combined_out_path = output_path_list[0].parent.parent / \
            f'{date.today()}_{prefix}_{num_vols}_volumes_combined.h5'
        if final:
            combined_out_path = output_path_list[0].parent / \
                f'{date.today()}_{prefix}_12_volumes_combined.h5'
        logging.info(f'Saving the {num_vols} combined volumes to {combined_out_path}')
        combined = combined
        combined = np.rot90(combined, 0 - k)
        with h5.File(combined_out_path, 'w') as f:
            f['/data'] = combined
        if self.delete_vols:
            logging.info("Deleting the source volumes for the combined volume")
            for vol_filepath in output_path_list:
                os.remove(vol_filepath)
        return combined_out_path

    def predict_single_slice(self, axis, index, data, output_path):
        """Takes in a 2d data array and saves the predicted U-net segmentation to disk.

        Args:
            axis (str): The name of the axis to incorporate in the output filename.
            index (int): The slice number to incorporate in the output filename.
            data (numpy.array): The 2d data array to be fed into the U-net.
            output_path (pathlib.Path): The path to directory for file output.
        """
        data = img_as_float(data)
        img = Image(pil2tensor(data, dtype=np.float32))
        self.fix_odd_sides(img)
        prediction = self.predictor.model.predict(img)
        pred_slice = img_as_ubyte(prediction[1][0])
        io.imsave(
            output_path/f"unet_prediction_{axis}_stack_{index}.png", pred_slice)

    def fix_odd_sides(self, example_image):
        """Replaces an an odd image dimension with an even dimension by padding.
    
        Taken from https://forums.fast.ai/t/segmentation-mask-prediction-on-different-input-image-sizes/44389/7.

        Args:
            example_image (fastai.vision.Image): The image to be fixed.
        """
        if (list(example_image.size)[0] % 2) != 0:
            example_image = crop_pad(example_image,
                                    size=(list(example_image.size)[
                                        0]+1, list(example_image.size)[1]),
                                    padding_mode='reflection')

        if (list(example_image.size)[1] % 2) != 0:
            example_image = crop_pad(example_image,
                                    size=(list(example_image.size)[0], list(
                                        example_image.size)[1] + 1),
                                    padding_mode='reflection')

    def predict_orthog_slices_to_disk(self, data_arr, output_path):
        """Outputs slices from data or ground truth seg volumes sliced in
         all three of the orthogonal planes
         
        Args:
        data_array (numpy.array): The 3d data volume to be sliced and predicted.
        output_path (pathlib.Path): A Path to the output directory.
         """
        shape_tup = data_arr.shape
        ax_idx_pairs = self.get_axis_index_pairs(shape_tup)
        num_ims = self.get_num_of_ims(shape_tup)
        for axis, index in tqdm(ax_idx_pairs, total=num_ims):
            self.predict_single_slice(
                axis, index, self.axis_index_to_slice(data_arr, axis, index), output_path)

    def consensus_threshold(self, input_path):
        """Saves a consensus thresholded volume from combination of binary volumes.

        Args:
            input_path (pathlib.Path): Path to the combined HDF5 volume that is
             to be thresholded.
        """
        for val in self.consensus_vals:
            combined = self.numpy_from_hdf5(input_path)
            combined_out = input_path.parent / \
                f'{date.today()}_combined_consensus_thresh_cutoff_{val}.h5'
            combined[combined < val] = 0
            combined[combined >= val] = 255
            logging.info(f'Writing to {combined_out}')
            with h5.File(combined_out, 'w') as f:
                f['/data'] = combined

    def predict_12_ways(self, root_path):
        """Runs the loop that coordinates the prediction of a 3d data volume
        by a 2d U-net in 12 orientations and then combination of the segmented
        binary outputs.

        Args:
            root_path (pathlib.Path): Path to the top level directory for data
            output.
        """
        self.setup_folder_stucture(root_path)
        combined_vol_paths = []
        for k in tqdm(range(4), ncols=100, desc='Total progress', postfix="\n"):
            key, output_path = self.dir_list[k]
            logging.info(f'Rotating volume {k * 90} degrees')
            rotated = np.rot90(self.data_vol, k)
            logging.info("Predicting slices to disk.")
            self.predict_orthog_slices_to_disk(rotated, output_path)
            output_path_list = self.combine_slices_to_vol(output_path)
            fp = self.combine_vols(output_path_list, k, key)
            combined_vol_paths.append(fp)
        # Combine all the volumes
        final_combined = self.combine_vols(combined_vol_paths, 0, 'final', True)
        self.consensus_threshold(final_combined)
        if self.delete_vols:
            for _, vol_dir in self.dir_list:
                os.rmdir(vol_dir)


class PredictionHDF5DataSlicer(PredictionDataSlicer):

    def __init__(self, settings, predictor, data_vol_path):
        logging.info(f"Using {self.__class__.__name__}")
        super().__init__(settings, predictor, data_vol_path)
        self.output_probs = settings.output_probs
        self.quality = settings.quality

    def create_target_hdf5_files(self, directory, shape_tup):
        for axis in ("z", "y", "x"):
            # Create an HDF5 volume with empty datasets for labels and probabilites
            with h5.File(directory/f"unet_prediction_{axis}_axis.h5", 'w') as f:
                f.create_dataset("labels", shape_tup, dtype='u1')
                f['/data'] = f['/labels']
                f.create_dataset("probabilities", shape_tup, dtype=np.float16)

    def setup_folder_stucture(self, root_path):
        """OVERRIDES METHOD IN PARENT CLASS.
        Sets up a folder structure to store the predicted images.

        Args:
            root_path (Path): The top level directory for data output.
        """
        vol_dir = root_path/f'{date.today()}_predicted_volumes'
        non_rotated = vol_dir/f'{date.today()}_non_rotated_volumes'
        rot_90_seg = vol_dir/f'{date.today()}_rot_90_volumes'
        rot_180_seg = vol_dir/f'{date.today()}_rot_180_volumes'
        rot_270_seg = vol_dir/f'{date.today()}_rot_270_volumes'

        self.dir_list = [
            ('non_rotated', non_rotated),
            ('rot_90_seg', rot_90_seg),
            ('rot_180_seg', rot_180_seg),
            ('rot_270_seg', rot_270_seg)
        ]
        for _, dir_path in self.dir_list:
            os.makedirs(dir_path, exist_ok=True)
            #self.create_target_hdf5_files(dir_path)

    def predict_single_slice(self, data):
        """OVERRIDES METHOD IN PARENT CLASS.
        Takes in a 2d data array and returns the max and argmax of the predicted probabilities.

        Args:
            data (numpy.array): The 2d data array to be fed into the U-net.

        Returns:
            torch.tensor: A 3d torch tensor containing a 2d array with max probabilities
            and a 2d array with argmax indices.
        """
        data = img_as_float(data)
        data = Image(pil2tensor(data, dtype=np.float32))
        self.fix_odd_sides(data)
        prediction = self.predictor.model.predict(data)[2]
        return torch.max(prediction, dim=0)
            
    def predict_orthog_slices_to_disk(self, data_arr, output_path, k):
        """OVERRIDES METHOD IN PARENT CLASS.
        Outputs slices from data or ground truth seg volumes sliced in
         all three of the orthogonal planes
         
        Args:
        data_array (numpy.array): The 3d data volume to be sliced and predicted.
        output_path (pathlib.Path): A Path to the output directory.
         """
        shape_tup = data_arr.shape
        # Axis by axis
        z_ax_idx_pairs = product('z', range(shape_tup[0]))
        y_ax_idx_pairs = product('y', range(shape_tup[1]))
        x_ax_idx_pairs = product('x', range(shape_tup[2]))
        # Create volumes for label and prob data
        logging.info("Creating empty data volumes in RAM")
        label_container = np.empty((2, *shape_tup), dtype=np.uint8)
        prob_container = np.empty((2, *shape_tup), dtype=np.float16)
        logging.info("Predicting Z slices:")
        for axis, index in tqdm(z_ax_idx_pairs, total=shape_tup[0]):
            prob, label = self.predict_single_slice(
                self.axis_index_to_slice(data_arr, axis, index))
            prob_container[0, index] = prob
            label_container[0, index] = label
        # Hacky output of first volume
        if k==0:
            fastz_out_path = output_path.parent/f"{self.data_vol_path.stem}_1_plane_prediction.h5"
            logging.info(f"Saving single plane prediction to {fastz_out_path}")
            self.save_data_to_hdf5(label_container, fastz_out_path)
            if self.quality == 'low':
                logging.info("Quality set to low. Ending processing.")
                sys.exit(0)
        logging.info("Predicting Y slices:")
        for axis, index in tqdm(y_ax_idx_pairs, total=shape_tup[1]):
            prob, label = self.predict_single_slice(
                self.axis_index_to_slice(data_arr, axis, index))
            prob_container[1, :, index] = prob
            label_container[1, :, index] = label
        logging.info("Merging Z and Y volumes.")
        # Merge these volumes and replace the volume at index 0 in the container
        self.merge_vols_in_mem(prob_container, label_container)
        logging.info("Predicting X slices:")
        for axis, index in tqdm(x_ax_idx_pairs, total=shape_tup[2]):
            prob, label = self.predict_single_slice(
                self.axis_index_to_slice(data_arr, axis, index))
            prob_container[1, :, :, index] = prob
            label_container[1, :, :, index]=label
        logging.info("Merging max of Z and Y with the X volume.")
        # Merge these volumes and replace the volume at index 0 in the container
        self.merge_vols_in_mem(prob_container, label_container)
        logging.info("Saving combined labels and probabilities to disk.")
        combined_out = output_path/"max_out.h5"
        with h5.File(combined_out, 'w') as f:
            f['/probabilities'] = prob_container[0]
            f['/labels'] = label_container[0]
            f['/data'] = f['/labels']
        if k == 0:
            fast3_vol_out_path = output_path.parent/f"{self.data_vol_path.stem}_3_plane_prediction.h5"
            logging.info(f"Saving 3 plane prediction to {fast3_vol_out_path}")
            self.save_data_to_hdf5(label_container, fast3_vol_out_path)
            if self.quality == 'medium':
                logging.info("Quality set to medium. Ending processing.")
                sys.exit(0)
        return combined_out

    def save_data_to_hdf5(self, label_container, out_path):
        chunking = self.input_data_chunking if self.input_data_chunking else True
        with h5.File(out_path, 'w') as f:
                    # Upsample segmentation if data has been downsampled
            if self.downsample:
                labels = self.upsample_segmentation(label_container[0])
            else:
                labels = label_container[0]
            f.create_dataset("/labels", data=labels, chunks=chunking, compression="gzip")
            f['/data'] = f['/labels']

    def merge_vols_in_mem(self, prob_container, label_container):
        max_prob_idx = np.argmax(prob_container, axis=0)
        max_prob_idx = max_prob_idx[np.newaxis, :, :, :]
        prob_container[0] = np.squeeze(np.take_along_axis(
            prob_container, max_prob_idx, axis=0))
        label_container[0] = np.squeeze(np.take_along_axis(
            label_container, max_prob_idx, axis=0))

    def hdf5_to_rotated_numpy(self, filepath, hdf5_path="/data"):
        with h5.File(filepath, 'r') as f:
            data = f[hdf5_path][()]
        # Find which rotation this data has been subjected to, rotate back
        fp_nums = re.findall(r"\d+", filepath.parent.name)
        if '90' in fp_nums:
            data = np.swapaxes(data, 0, 1)
            data = np.fliplr(data)
        elif '180' in fp_nums:
            data = np.rot90(data, 2, (0, 1))
        elif '270' in fp_nums:
            data = np.fliplr(data)
            data = np.swapaxes(data, 0, 1)
        return data

    def merge_final_vols(self, file_list):

        output_path = file_list[0].parent
        combined_label_out_path = output_path.parent / \
                f"{self.data_vol_path.stem}_12_plane_prediction.h5"
        combined_prob_out_path = output_path.parent / \
            f"{self.data_vol_path.stem}_12_plane_prediction_probs.h5"
        logging.info("Merging final output data using maximum probabilties:")
        logging.info("Creating empty data volumes in RAM")
        label_container = np.empty((2, *self.data_vol_shape), dtype=np.uint8)
        prob_container = np.empty((2, *self.data_vol_shape), dtype=np.float16)
        logging.info(f"Starting with {file_list[0].parent.name} {file_list[0].name}")
        prob_container[0] = self.hdf5_to_rotated_numpy(
            file_list[0], '/probabilities')
        label_container[0] = self.hdf5_to_rotated_numpy(
            file_list[0], '/labels')
        for subsequent in file_list[1:]:
            logging.info(f"Merging with {subsequent.parent.name} {subsequent.name}")
            prob_container[1] = self.hdf5_to_rotated_numpy(
                subsequent, '/probabilities')
            label_container[1] = self.hdf5_to_rotated_numpy(
                subsequent, '/labels')
            self.merge_vols_in_mem(prob_container, label_container)
        logging.info(f"Saving final data out to: {combined_label_out_path}.")
        self.save_data_to_hdf5(label_container, combined_label_out_path)
        if self.output_probs:
            logging.info(f"Saving final probabilities out to: {combined_prob_out_path}.")
            chunking = self.input_data_chunking if self.input_data_chunking else True
            with h5.File(combined_prob_out_path, 'w') as f:
                    # Upsample segmentation if data has been downsampled
                probs = prob_container[0]
                f.create_dataset("/probabilities", data=probs, chunks=chunking, compression="gzip")
                f['/data'] = f["/probabilities"]

    def tile_array(self, a, b0, b1, b2):
        """Fast method for upsampling a segmentation by repeating values.
        Modified from https://stackoverflow.com/a/52341775"""
        h, r, c = a.shape
        out = np.empty((h, b0, r, b1, c, b2), a.dtype)
        out[...] = a[:, None, :, None, :, None]
        return out.reshape(h*b0, r*b1, c*b2)

    def upsample_segmentation(self, data, factor=2):
        logging.info(f"Upsampling segmentation by a factor of {factor}.")
        return self.tile_array(data, factor, factor, factor)

    def predict_12_ways(self, root_path):
        """OVERRIDES METHOD IN PARENT CLASS.
        Runs the loop that coordinates the prediction of a 3d data volume
        by a 2d U-net in 12 orientations and then combination of the segmented
        binary outputs.

        Args:
            root_path (pathlib.Path): Path to the top level directory for data
            output.
        """
        self.setup_folder_stucture(root_path)
        combined_vol_paths = []
        for k in tqdm(range(4), ncols=100, desc='Total progress', postfix="\n"):
            _, output_path = self.dir_list[k]
            logging.info(f'Rotating volume {k * 90} degrees')
            rotated = np.rot90(self.data_vol, k)
            logging.info("Predicting slices to HDF5 files.")
            fp =  self.predict_orthog_slices_to_disk(rotated, output_path, k)
            combined_vol_paths.append(fp)
        # Combine all the volumes
        self.merge_final_vols(combined_vol_paths)
        if self.delete_vols:
            # Remove source volumes
            logging.info("Removing maximum probability h5 files.")
            for h5_file in combined_vol_paths:
                os.remove(h5_file)
            for _, vol_dir in self.dir_list:
                os.rmdir(vol_dir)
