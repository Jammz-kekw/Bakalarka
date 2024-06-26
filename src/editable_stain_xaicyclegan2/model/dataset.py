"""
    Prevzatý kód
"""

from PIL import Image
import kornia.color
import torch.utils.data as data
from torchvision import transforms
import kornia
import os
import random


class LabNormalize:
    def __init__(self, l_mean: float = 50, l_std: float = 29.59, ab_mean: float = 0, ab_std: float = 74.04):
        """
        :param l_mean: mean value for L channel
        :param l_std: std value for L channel
        :param ab_mean: mean value for a,b channel
        :param ab_std: std value for a,b channel
        """
        self.l_mean = l_mean
        self.l_std = l_std
        self.ab_mean = ab_mean
        self.ab_std = ab_std

    def __call__(self, tensor):
        """
        :param tensor: Tensor image of size (C, H, W) to be normalized.
        :return: Normalized Tensor image.
        """

        tensor[0] = (tensor[0] - self.l_mean) / self.l_std
        tensor[1] = (tensor[1] - self.ab_mean) / self.ab_std
        tensor[2] = (tensor[2] - self.ab_mean) / self.ab_std

        return tensor


class DefaultTransform:

    def __init__(self, norm_dict=None):
        """
        Serves as the default transform for the dataset, which only
        includes transforms.ToTensor() and transforms.Normalize()
        :param norm_dict: dictionary with mean and std values for normalization
        """

        self.norm_dict = norm_dict
        self.transform = None

        if self.norm_dict is None:
            self.norm_dict = {
                'mean': [0.5, 0.5, 0.5],
                'std': [0.5, 0.5, 0.5]
            }
        
        self.init()

    def __call__(self, img):
        return self.transform(img)

    def init(self):

        self.transform = transforms.Compose([
            transforms.ToTensor(),
            kornia.color.rgb_to_lab,
            LabNormalize(),
        ])


# Not my code, but I'm using it for the dataset
class DatasetFromFolder(data.Dataset):
    def __init__(
            self,
            image_dir: str,
            sub_folder: str,
            transform_norm_dict: dict = None,
            transform: DefaultTransform = None,
            resize: int = 256,
            crop_size: int = None,
            flip_h: bool = True,
            flip_v: bool = True
    ):

        """
        Dataset class for loading images from a folder to use while training

        :param image_dir: path to the folder containing the images, ideally with subfolders for train and test
        :param sub_folder: sub-folder to load images from, e.g. 'train/p63'
        :param transform: transform to apply to the images
        :param transform_norm_dict: dictionary with mean and std values for normalization
        :param resize: resize the images to the size of width and height specified by this argument
        :param crop_size: crop the images to the size of width and height specified by this argument
        :param flip_h: flip the images horizontally with a 50% chance
        :param flip_v: flip the images vertically with a 50% chance
        """

        super(DatasetFromFolder, self).__init__()

        self.input_path = os.path.join(image_dir, sub_folder)
        self.image_filenames = [x for x in sorted(os.listdir(self.input_path))]
        self.seq = 0
        self.seq2 = 0
        self.resize = resize
        self.crop_size = crop_size
        self.flip_h = flip_h
        self.flip_v = flip_v

        if transform is None:
            self.transform = DefaultTransform(transform_norm_dict)

    def __getitem__(self, index):

        while True:
            try:
                img_fn = os.path.join(self.input_path, self.image_filenames[index])
                img = Image.open(img_fn).convert('RGB')
            except (OSError, SyntaxError) as e:
                print(e)
                print("Deleting it.")
                os.remove(img_fn)
                self.image_filenames.pop(index)
                continue
            except IndexError:
                # change index to random one
                index = random.randint(0, len(self.image_filenames) - 1)
            else:
                break

        # preprocessing
        if self.resize:
            img = img.resize((self.resize, self.resize), Image.BILINEAR)

        if self.crop_size:
            x = random.randint(0, self.resize - self.crop_size + 1)
            y = random.randint(0, self.resize - self.crop_size + 1)
            img = img.crop((x, y, x + self.crop_size, y + self.crop_size))

        # flipping switched off in order to keep parity orientation (may have impact on image quality)
        """ 
        if self.flip_h:
            if random.random() < 0.5:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)

        if self.flip_v:
            if random.random() < 0.5:
                img = img.transpose(Image.FLIP_TOP_BOTTOM)
        """

        if self.transform is not None:
            img = self.transform(img)

        return img

    def __len__(self):
        return len(self.image_filenames)

    def get_random_image(self):
        return self.__getitem__(random.randint(0, len(self.image_filenames) - 1))

    def get_sequential_image(self):
        img = self.__getitem__(self.seq)
        self.seq += 1
        if self.seq >= len(self.image_filenames):
            self.seq = 0
        return img

    def get_sequential_image2(self):
        img = self.__getpic__(self.seq2)
        self.seq2 += 1
        if self.seq2 >= len(self.image_filenames):
            self.seq2 = 0
        return img

    def __getpic__(self, index):

        while True:
            try:
                img_fn = os.path.join(self.input_path, self.image_filenames[index])
                img = Image.open(img_fn).convert('RGB')
            except (OSError, SyntaxError) as e:
                print(e)
                print("Deleting it.")
                os.remove(img_fn)
                self.image_filenames.pop(index)
                continue
            except IndexError:
                # change index to random one
                index = random.randint(0, len(self.image_filenames) - 1)
            else:
                break

        # preprocessing
        if self.resize:
            img = img.resize((self.resize, self.resize), Image.BILINEAR)

        if self.crop_size:
            x = random.randint(0, self.resize - self.crop_size + 1)
            y = random.randint(0, self.resize - self.crop_size + 1)
            img = img.crop((x, y, x + self.crop_size, y + self.crop_size))

        if self.transform is not None:
            img = self.transform(img)

        return img
