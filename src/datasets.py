import copy
import os
import torchvision.datasets as datasets
from torch.utils.data import DataLoader
import torchvision
import torch
import subprocess
from glob import glob

from src.utils import AugmentationSwitch, DatasetSwitch
from src import paths

registered_datasets = {}


class GaussianISONoise(torch.nn.Module):
    """Add Gaussian noise to an image with a given standard deviation.
    Args:
        std (float): standard deviation of the Gaussian noise
    """

    def __init__(self, std: float):
        super().__init__()
        self.std = std

    def forward(self, in_tensor: torch.Tensor) -> torch.Tensor:
        return in_tensor + torch.randn_like(in_tensor) * self.std

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(sigma={self.p})"

    def __str__(self):
        return super().__str__() + f" with std={self.std}"


def register_dataset(name):
    def decorator(func):
        str_name = str(name)
        # we make sure that the path is set in paths.py
        func.__root_path__ = getattr(paths, f"{str_name.upper()}_ROOT")
        # we register the function
        registered_datasets[name] = func
        return func

    return decorator


def move_output_compute_node(COMPUTE_OUTPUT_DIR, experiment_output_dir):

    result = subprocess.run(
        [
            "time",
            "fpsync",
            "-n",
            "8",
            "-m",
            "tarify",
            "-s",
            "2000M",
            COMPUTE_OUTPUT_DIR,
            experiment_output_dir,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to sync output: {result.stderr}")


def resolve_data_directories(args):
    DATA_DIR = registered_datasets[args["dataset"]].__root_path__

    # If port is 0, we are debugging locally
    if args["port"] == 0:
        # Running locally
        COMPUTE_DATA_DIR = paths.get_local_data_dir(args["dataset"])
        COMPUTE_OUTPUT_DIR = paths.LOCAL_OUTPUT_DIR
    else:
        # Running on a compute node
        COMPUTE_DATA_DIR = paths.get_remote_data_dir(args["dataset"])
        COMPUTE_OUTPUT_DIR = paths.COMPUTE_OUTPUT_DIR

    LOCAL_OUTPUT_DIR = paths.LOCAL_OUTPUT_DIR

    if DATA_DIR.endswith(".tgz"):
        EXT = "tgz"
        BASE_DIR = os.path.basename(DATA_DIR).replace(".tgz", "")
    else:
        EXT = "tar"
        BASE_DIR = os.path.basename(DATA_DIR)

    COMPUTE_DATA_DIR_BASE_DIR = os.path.join(COMPUTE_DATA_DIR, BASE_DIR)
    os.makedirs(COMPUTE_DATA_DIR_BASE_DIR, exist_ok=True)
    TARGET_DIR = COMPUTE_DATA_DIR_BASE_DIR if EXT == "tar" else COMPUTE_DATA_DIR

    os.makedirs(COMPUTE_OUTPUT_DIR, exist_ok=True)
    return (
        DATA_DIR,
        COMPUTE_DATA_DIR,
        EXT,
        COMPUTE_DATA_DIR_BASE_DIR,
        TARGET_DIR,
        COMPUTE_OUTPUT_DIR,
        LOCAL_OUTPUT_DIR,
    )


def extract_the_dataset_on_compute_node(
    COMPUTE_DATA_DIR,
    EXT,
    COMPUTE_DATA_DIR_BASE_DIR,
):
    result = subprocess.run(
        f"time ls {COMPUTE_DATA_DIR}*.{EXT} | xargs -n 1 -P 8 -I @ tar -xf @ -C {COMPUTE_DATA_DIR_BASE_DIR}",
        capture_output=True,
        text=True,
        shell=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to extract data")


def move_data_to_compute_node(
    DATA_DIR,
    IS_COMPRESSED,
    COMPUTE_DATA_DIR,
):
    if IS_COMPRESSED:
        result = subprocess.run(
            [
                "time",
                "rsync",
                "-avh",
                "--progress",
                DATA_DIR,
                COMPUTE_DATA_DIR,
            ],
            capture_output=True,
            text=True,
        )
    else:
        result = subprocess.run(
            [
                "time",
                "fpsync",
                "-n",
                "8",
                "-m",
                "tarify",
                "-s",
                "2000M",
                DATA_DIR,
                COMPUTE_DATA_DIR,
            ],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to sync data: {result.stderr}")


class RepeatedSequentialSampler(torch.utils.data.Sampler):
    """Wraps another sampler to yield a minibatch of indices multiple times.
    Args:
        datasource (torch.utils.data.Dataset): The dataset to sample from.
        num_repeats (int): Number of times to repeat the indices.
    """

    def __init__(self, datasource: torch.utils.data.Dataset, num_repeats: int):
        self.sampler = torch.utils.data.SequentialSampler(datasource)
        self.num_repeats = num_repeats

    def __iter__(self):
        for s in self.sampler:
            for _ in range(self.num_repeats):
                yield s

    def __len__(self):
        return len(self.sampler) * self.num_repeats


def get_training_and_test_dataloader(
    dataset,
    root_path,
    batch_size,
    num_workers=2,
    prefetch_factor=4,
    get_only_test=False,
    shuffle=True,
    sampler=None,
    **dataset_kwargs,
):

    if dataset not in registered_datasets:
        raise ValueError(
            f"Dataset {dataset} not found. Available datasets: {registered_datasets.keys()}"
        )
    # dict lookup instead of if-else
    training_data, test_data = registered_datasets[dataset](
        root_path=root_path,
        get_only_test=get_only_test,
        **dataset_kwargs,
    )
    num_classes = len(test_data.classes)
    input_shape = test_data[0][0].shape
    test_sampler = None if sampler is None else sampler(test_data)

    # DATA LOADER
    test_dataloader = DataLoader(
        test_data,
        batch_size=batch_size,
        num_workers=num_workers,
        prefetch_factor=prefetch_factor,
        # pin_memory=True,
        sampler=None if test_sampler is None else test_sampler,
    )

    if get_only_test:
        return test_dataloader, input_shape, num_classes

    train_sampler = None if sampler is None else sampler(training_data)

    train_dataloader = DataLoader(
        training_data,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=num_workers,
        prefetch_factor=prefetch_factor,
        # pin_memory=True,
        sampler=None if train_sampler is None else train_sampler,
    )

    return train_dataloader, test_dataloader, input_shape, num_classes


CIFAR10_MEAN = (0.49139968, 0.48215841, 0.44653091)
CIFAR10_STD = (0.24703223, 0.24348513, 0.26158784)
IMAGENETTE_MEAN = (0.485, 0.456, 0.406)
IMAGENETTE_STD = (0.229, 0.224, 0.225)
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
FASHION_MNIST_MEAN = (0.5,)
FASHION_MNIST_STD = (0.5,)
MNIST_MEAN = (0.5,)
MNIST_STD = (0.5,)


# see b-cos v2 for this
# We have added this to do an ablation study
# turns out not important enough to be in the paper.
class AddInverse(torch.nn.Module):
    """To a [B, C, H, W] input add the inverse channels of the given one to it.
    Results in a [B, 2C, H, W] output. Single image [C, H, W] is also accepted.

    Args:
        dim (int): where to add channels to. Default: -3
    """

    def __init__(self, dim: int = -3):
        super().__init__()
        self.dim = dim

    def forward(self, in_tensor: torch.Tensor) -> torch.Tensor:
        return torch.cat([in_tensor, 1 - in_tensor], dim=self.dim)


@register_dataset(DatasetSwitch.IMAGENET)
def get_imagenet_dataset(
    root_path,
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation,
    get_only_test,
    **kwargs,
):
    img_size = 224 if img_size is None else img_size
    label_transform = None
    test_data = get_imagenet_test(
        root_path,
        img_size,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        augmentation,
        label_transform,
    )

    if get_only_test:
        return None, test_data

    training_data = get_imagenet_train(
        root_path,
        img_size,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        augmentation,
        label_transform,
    )

    return training_data, test_data


def get_imagenet_train(
    root_path,
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation,
    label_transform=None,
):
    assert isinstance(
        augmentation, AugmentationSwitch
    ), f"Augmentation must be an enum of type AugmentationSwitch"

    augmentations_ = get_aug_imagenet(
        img_size,
        augmentation,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        "train",
    )

    training_data = datasets.ImageNet(
        root=root_path,
        split="train",
        transform=augmentations_,
        target_transform=label_transform,
    )

    return training_data


def get_imagenet_test(
    root_path,
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation=AugmentationSwitch.TRAIN,
    label_transform=None,
):
    assert isinstance(
        augmentation, AugmentationSwitch
    ), f"Augmentation must be an enum of type AugmentationSwitch"

    augmentations_ = get_aug_imagenet(
        img_size,
        augmentation,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        "test",
    )

    test_data = datasets.ImageNet(
        root=root_path,
        split="val",
        transform=augmentations_,
        target_transform=label_transform,
    )

    return test_data


def get_aug_imagenet(
    img_size,
    augmentation,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    split,
):
    if augmentation == AugmentationSwitch.TRAIN:
        if split == "train":
            augmentations = [
                torchvision.transforms.ToTensor(),
                torchvision.transforms.RandomResizedCrop(img_size),
                torchvision.transforms.RandomChoice(
                    [
                        torchvision.transforms.RandomHorizontalFlip(),
                        torchvision.transforms.RandomVerticalFlip(),
                        torchvision.transforms.ColorJitter(
                            brightness=0.1, contrast=0.1, saturation=0.1, hue=0.1
                        ),
                        torchvision.transforms.RandomRotation(10),
                        torchvision.transforms.RandomAffine(
                            degrees=5, translate=(0.1, 0.1)
                        ),
                        torchvision.transforms.RandomPerspective(distortion_scale=0.1),
                        torchvision.transforms.RandomErasing(p=0.25, value="random"),
                        torchvision.transforms.RandomGrayscale(p=0.1),
                    ]
                ),
                # ablation of Bcos
                (
                    AddInverse()
                    if add_inverse
                    else torchvision.transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD)
                ),
                GaussianISONoise(gaussian_noise_var),
            ]
            if gaussian_blur_var > 0:
                augmentations.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )
        elif split == "test":
            augmentations = [
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Resize((img_size, img_size)),
                (
                    # ablation of Bcos
                    AddInverse()
                    if add_inverse
                    else torchvision.transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD)
                ),
            ]
        else:
            raise ValueError(f"Split {split} not recognized")
    elif augmentation == AugmentationSwitch.EXP_GEN:
        augmentations = [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
            (
                # ablation of Bcos
                AddInverse()
                if add_inverse
                else torchvision.transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD)
            ),
        ]
        if gaussian_noise_var > 0:
            augmentations.append(GaussianISONoise(gaussian_noise_var))
        if gaussian_blur_var > 0:
            augmentations.append(
                torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
            )
    if augmentation == AugmentationSwitch.EXP_VIS:
        augmentations = (
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
        )

    assert len(augmentations) > 0, "Augmentations list is empty"
    augmentations = torchvision.transforms.Compose(augmentations)
    return augmentations


@register_dataset(DatasetSwitch.IMAGENETTE)
def get_imagenette_dataset(
    root_path,
    img_size,
    augmentation,
    gaussian_noise_var,
    gaussian_blur_var,
    add_inverse=False,
    **kwargs,
):
    img_size = 224 if img_size is None else img_size
    label_transform = None
    training_data = get_imagenette_train(
        root_path,
        img_size,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        augmentation,
        label_transform,
    )
    test_data = get_imagenette_test(
        root_path,
        img_size,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        augmentation,
        label_transform,
    )

    return training_data, test_data


def get_imagenette_train(
    root_path,
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation,
    label_transform=None,
):
    assert isinstance(
        augmentation, AugmentationSwitch
    ), f"Augmentation must be an enum of type AugmentationSwitch"

    augmentations_ = get_aug_imagenette(
        img_size,
        augmentation,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        "train",
    )

    training_data = datasets.Imagenette(
        root=root_path,
        split="train",
        transform=augmentations_,
        target_transform=label_transform,
        download=False,
    )

    return training_data


def get_aug_imagenette(
    img_size,
    augmentation,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    split,
):
    if augmentation == AugmentationSwitch.TRAIN:
        if split == "train":
            augmentations = [
                torchvision.transforms.ToTensor(),
                torchvision.transforms.RandomResizedCrop(img_size),
                torchvision.transforms.RandomChoice(
                    [
                        torchvision.transforms.RandomHorizontalFlip(),
                        torchvision.transforms.RandomVerticalFlip(),
                        torchvision.transforms.ColorJitter(
                            brightness=0.1, contrast=0.1, saturation=0.1, hue=0.1
                        ),
                        torchvision.transforms.RandomRotation(10),
                        torchvision.transforms.RandomAffine(
                            degrees=5, translate=(0.1, 0.1)
                        ),
                        torchvision.transforms.RandomPerspective(distortion_scale=0.1),
                        torchvision.transforms.RandomErasing(p=0.25, value="random"),
                        torchvision.transforms.RandomGrayscale(p=0.1),
                    ]
                ),
                # ablation of Bcos
                (
                    AddInverse()
                    if add_inverse
                    else torchvision.transforms.Normalize(
                        IMAGENETTE_MEAN, IMAGENETTE_STD
                    )
                ),
                GaussianISONoise(gaussian_noise_var),
            ]
            if gaussian_blur_var > 0:
                augmentations.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )
        elif split == "test":
            augmentations = [
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Resize((img_size, img_size)),
                (
                    # ablation of Bcos
                    AddInverse()
                    if add_inverse
                    else torchvision.transforms.Normalize(
                        IMAGENETTE_MEAN, IMAGENETTE_STD
                    )
                ),
            ]
        else:
            raise ValueError(f"Split {split} not recognized")

    elif augmentation == AugmentationSwitch.EXP_GEN:
        augmentations = [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
            (
                # ablation of Bcos
                AddInverse()
                if add_inverse
                else torchvision.transforms.Normalize(IMAGENETTE_MEAN, IMAGENETTE_STD)
            ),
            GaussianISONoise(gaussian_noise_var),
        ]
        if gaussian_blur_var > 0:
            augmentations.append(
                torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
            )
    if augmentation == AugmentationSwitch.EXP_VIS:
        augmentations = (
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
        )
    augmentations = torchvision.transforms.Compose(augmentations)
    return augmentations


def get_imagenette_test(
    root_path,
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation=AugmentationSwitch.TRAIN,
    label_transform=None,
):
    assert isinstance(
        augmentation, AugmentationSwitch
    ), f"Augmentation must be an enum of type AugmentationSwitch"

    augmentations_ = get_aug_imagenette(
        img_size,
        augmentation,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        "test",
    )

    test_data = datasets.Imagenette(
        root=root_path,
        split="val",
        transform=augmentations_,
        target_transform=label_transform,
        download=False,
    )

    return test_data


@register_dataset(DatasetSwitch.FASHION_MNIST)
def get_fashion_mnist_dataset(
    root_path,
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation=AugmentationSwitch.TRAIN,
    label_transform=None,
):
    img_size = 28 if img_size is None else img_size
    test_transform, train_transform = get_aug_fmnist(
        img_size,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        augmentation,
    )

    label_dtype = torch.float32
    label_transform = None

    training_data = datasets.FashionMNIST(
        root=root_path,
        train=True,
        download=False,
        transform=train_transform,
        target_transform=label_transform,
    )

    test_data = datasets.FashionMNIST(
        root=root_path,
        train=False,
        download=False,
        transform=test_transform,
        target_transform=label_transform,
    )

    return training_data, test_data


def get_aug_fmnist(
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation,
):
    if augmentation == AugmentationSwitch.EXP_VIS:
        test_transform = [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
        ]
        train_transform = test_transform.copy()
        if add_inverse:
            raise NotImplementedError("AddInverse not implemented for FashionMNIST")
    else:
        test_transform = [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Normalize(FASHION_MNIST_MEAN, FASHION_MNIST_STD),
            torchvision.transforms.Resize((img_size, img_size)),
        ]
        train_transform: list = test_transform.copy()
        if augmentation == AugmentationSwitch.TRAIN:
            if gaussian_noise_var > 0:
                train_transform.append(GaussianISONoise(gaussian_noise_var))
            if gaussian_blur_var > 0:
                train_transform.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )
            if add_inverse:
                raise NotImplementedError("AddInverse not implemented for FashionMNIST")
        elif augmentation == AugmentationSwitch.EXP_GEN:
            if gaussian_noise_var > 0:
                train_transform.append(GaussianISONoise(gaussian_noise_var))
                test_transform.append(GaussianISONoise(gaussian_noise_var))
            if gaussian_blur_var > 0:
                train_transform.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )
                test_transform.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )

    test_transform = torchvision.transforms.Compose(test_transform)
    train_transform = torchvision.transforms.Compose(train_transform)
    return test_transform, train_transform


@register_dataset(DatasetSwitch.CIFAR10)
def get_cifar10_dataset(
    root_path,
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation=AugmentationSwitch.TRAIN,
    label_transform=None,
):
    img_size = 32 if img_size is None else img_size
    label_transform = None

    train_transform, test_transform = get_aug_cifar10(
        img_size,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        augmentation,
    )
    training_data = datasets.CIFAR10(
        root=root_path,
        train=True,
        download=False,
        transform=train_transform,
        target_transform=label_transform,
    )
    test_data = datasets.CIFAR10(
        root=root_path,
        train=False,
        download=False,
        transform=test_transform,
        target_transform=label_transform,
    )

    return training_data, test_data


def get_aug_cifar10(
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation,
):
    if augmentation == AugmentationSwitch.EXP_VIS:
        test_transform = [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
        ]
        train_transform = test_transform.copy()
        if add_inverse:
            raise NotImplementedError("AddInverse not implemented for CIFAR10")
    else:
        test_transform = [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
            (
                AddInverse()
                if add_inverse
                else torchvision.transforms.Normalize(CIFAR10_MEAN, CIFAR10_STD)
            ),
        ]
        train_transform = test_transform.copy()
        if augmentation == AugmentationSwitch.TRAIN:
            train_transform.append(
                torchvision.transforms.RandomHorizontalFlip(),
            )
            if gaussian_noise_var > 0:
                train_transform.append(GaussianISONoise(gaussian_noise_var))
            if gaussian_blur_var > 0:
                train_transform.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )
        elif augmentation == AugmentationSwitch.EXP_GEN:
            if gaussian_noise_var > 0:
                train_transform.append(GaussianISONoise(gaussian_noise_var))
                test_transform.append(GaussianISONoise(gaussian_noise_var))
            if gaussian_blur_var > 0:
                train_transform.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )
                test_transform.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )
    train_transform = torchvision.transforms.Compose(train_transform)
    test_transform = torchvision.transforms.Compose(test_transform)
    return train_transform, test_transform


def get_aug_mnist(
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation,
):
    if augmentation == AugmentationSwitch.EXP_VIS:
        test_transform = [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
        ]
        train_transform = test_transform.copy()
        if add_inverse:
            raise NotImplementedError("AddInverse not implemented for MNIST")
    else:
        test_transform = [
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize((img_size, img_size)),
            (
                AddInverse()
                if add_inverse
                else torchvision.transforms.Normalize(MNIST_MEAN, MNIST_STD)
            ),
        ]
        train_transform = test_transform.copy()
        if gaussian_noise_var > 0:
            train_transform.append(GaussianISONoise(gaussian_noise_var))
            if augmentation == AugmentationSwitch.EXP_GEN:
                test_transform.append(GaussianISONoise(gaussian_noise_var))

        if gaussian_blur_var > 0:
            train_transform.append(
                torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
            )
            if augmentation == AugmentationSwitch.EXP_GEN:
                test_transform.append(
                    torchvision.transforms.GaussianBlur(5, gaussian_blur_var)
                )

    train_transform = torchvision.transforms.Compose(train_transform)
    test_transform = torchvision.transforms.Compose(test_transform)
    return train_transform, test_transform


@register_dataset(DatasetSwitch.MNIST)
def get_mnist_dataset(
    root_path,
    img_size,
    add_inverse,
    gaussian_noise_var,
    gaussian_blur_var,
    augmentation,
    **kwargs,
):
    img_size = 28 if img_size is None else img_size
    train_transform, test_transform = get_aug_mnist(
        img_size,
        add_inverse,
        gaussian_noise_var,
        gaussian_blur_var,
        augmentation,
    )

    label_dtype = torch.float32
    # label_transform = lambda y: torch.tensor([y <= 4, y > 4], dtype=label_dtype)
    label_transform = None

    training_data = datasets.MNIST(
        root=root_path,
        train=True,
        download=False,
        transform=train_transform,
        target_transform=label_transform,
    )

    test_data = datasets.MNIST(
        root=root_path,
        train=False,
        download=False,
        transform=test_transform,
        target_transform=label_transform,
    )

    return training_data, test_data


class GradsDataset(torch.utils.data.Dataset):
    def __init__(self, root_path):
        self.root_path = root_path
        self.files = glob(
            os.path.join(root_path, "**/*.pt"),
            recursive=True,
        )

    def __getitem__(self, idx):
        file_path = self.files[idx]
        data = torch.load(file_path)
        data["address"] = file_path
        return data

    def __len__(self):
        return len(self.files)


@register_dataset(DatasetSwitch.GRADS)
def get_grad_dataloader(root_path, num_workers, prefetch_factor):
    data = GradsDataset(root_path)
    dataloader = DataLoader(
        data,
        batch_size=1,
        shuffle=False,
        num_workers=num_workers,
        prefetch_factor=prefetch_factor,
    )
    return dataloader
