# Copyright 2024 PKU-Alignment Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


import os
from typing import Any, Callable
from typing_extensions import TypedDict  # Python 3.10+

import torch
import transformers
from torch.utils.data import Dataset
from torchvision import transforms
from tqdm import tqdm
from transformers.tokenization_utils import PaddingStrategy, TruncationStrategy

from align_anything.utils.multi_process import get_current_device, is_main_process
from align_anything.utils.tools import convert_to_rgb, ends_with_any, left_padding, right_padding
from datasets import load_dataset


__all__ = [
    'PreferenceDataset',
    'PreferenceCollator',
    'PreferenceSample',
    'PreferenceBatch',
    'SafetyPreferenceDataset',
    'SafetyPreferenceCollator',
    'SafetyPreferenceSample',
    'SafetyPreferenceBatch',
]


class PreferenceSample(TypedDict, total=True):
    input_ids: torch.LongTensor  # size = (L,)
    labels: torch.LongTensor  # size = (L,)
    pixel_values: torch.LongTensor | None  # size = (B, C, H, W)


class PreferenceBatch(TypedDict, total=True):
    input_ids: torch.LongTensor  # size = (B, L)
    labels: torch.LongTensor  # size = (B, L)
    attention_mask: torch.BoolTensor  # size = (B, L)
    pixel_values: torch.LongTensor | None  # size = (B, C, H, W)
    images_seq_mask: torch.BoolTensor | None  # size = (B, L)
    images_emb_mask: torch.BoolTensor | None  # size = (B, N)


class SafetyPreferenceSample(TypedDict, total=True):
    input_ids: torch.LongTensor  # size = (L,)
    labels: torch.LongTensor  # size = (L,)
    pixel_values: torch.LongTensor | None  # size = (B, C, H, W)
    is_better_safe: str
    is_worse_safe: str


class SafetyPreferenceBatch(TypedDict, total=True):
    input_ids: torch.LongTensor  # size = (B, L)
    labels: torch.LongTensor  # size = (B, L)
    attention_mask: torch.BoolTensor  # size = (B, L)
    pixel_values: torch.LongTensor | None  # size = (B, C, H, W)
    is_better_safe: list  # size = (B)
    is_worse_safe: list  # size = (B)


class PreferenceDataset(Dataset):

    def __init__(
        self,
        path: str,
        template: str,
        tokenizer: transformers.PreTrainedTokenizer,
        processor: transformers.ProcessorMixin | transforms.Compose | None = None,
        name: str | None = None,
        size: int | None = None,
        split: str | None = None,
        data_files: str | None = None,
        optional_args: list | str = [],
    ):
        super().__init__()
        assert path, f'You must set the valid datasets path! Here is {path}'
        assert template, f'You must set the valid template path! Here is {template}'
        self.tokenizer = tokenizer
        self.processor = processor
        self.template = template

        if isinstance(optional_args, str):
            optional_args = [optional_args]
        self.raw_data = load_dataset(
            path,
            name=name,
            split=split,
            data_files=data_files,
            *optional_args,
            trust_remote_code=True,
            verification_mode='no_checks',
        )
        if size:
            size = min(size, len(self.raw_data))
            self.raw_data = self.raw_data.select(range(int(size)))
        self.valid_indices = self.filter_indices()

    def filter_indices(self):
        valid_indices = []
        for i, item in tqdm(
            enumerate(self.raw_data),
            disable=not is_main_process(),
            total=len(self.raw_data),
            desc='Filtering valid indices',
        ):
            if not hasattr(self.template, 'check_equal'):
                valid_indices.append(i)
                continue

            if not self.template.check_equal(item):
                if hasattr(self.template, 'check_validation'):
                    if not self.template.check_validation(item):
                        continue
                valid_indices.append(i)
        return valid_indices

    def preprocess(self, raw_sample: dict[str, Any]) -> PreferenceSample:
        better_conversation, worse_conversation, meta_info = self.template.format_preference_sample(
            raw_sample
        )
        better_conversation = (
            better_conversation + self.tokenizer.eos_token
            if not ends_with_any(better_conversation, self.tokenizer.eos_token)
            else better_conversation
        )
        worse_conversation = (
            worse_conversation + self.tokenizer.eos_token
            if not ends_with_any(worse_conversation, self.tokenizer.eos_token)
            else worse_conversation
        )
        return_dict = {}
        return_dict['better_response_lens'] = len(
            self.tokenize(meta_info['better_response'], {}, add_special_tokens=False)['input_ids'][
                0
            ]
        )
        return_dict['worse_response_lens'] = len(
            self.tokenize(meta_info['worse_response'], {}, add_special_tokens=False)['input_ids'][0]
        )
        return_dict['better_conversation'] = better_conversation
        return_dict['worse_conversation'] = worse_conversation
        return_dict['image'] = meta_info['image']

        return return_dict

    def get_collator(self) -> Callable[[list[dict[str, torch.Tensor]]], dict[str, torch.Tensor]]:
        return PreferenceCollator(
            self.tokenizer.pad_token_id, self.processor, self.tokenizer.padding_side
        )

    def tokenize(
        self,
        conversation: str,
        meta_info: dict[str, Any],
        add_special_tokens: bool = True,
        padding: bool | str | PaddingStrategy = PaddingStrategy.DO_NOT_PAD,
        truncation: bool | str | TruncationStrategy = TruncationStrategy.LONGEST_FIRST,
        max_length: int | None = None,
    ) -> torch.LongTensor:  # size = (L,)
        """Tokenize a text string into a tensor representation."""
        if max_length is None:
            max_length = self.tokenizer.model_max_length

        return self.processor(
            text=conversation,
            images=meta_info.get('image', None),
            add_special_tokens=add_special_tokens,
            padding=padding,
            max_length=max_length,
            truncation=truncation,
            return_tensors='pt',
        )

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        """Get a tokenized data sample by index."""
        raw_sample = self.raw_data[self.valid_indices[index]]
        data = self.preprocess(raw_sample)
        return data

    def __len__(self) -> int:
        """Get the number of samples in the dataset."""
        return len(self.valid_indices)


class PreferenceCollator:

    def __init__(
        self,
        pad_token_id: int,
        processor: transformers.ProcessorMixin | transforms.Compose | None = None,
        padding_side: str = 'right',
    ) -> None:
        """Initialize a collator."""
        self.pad_token_id = pad_token_id
        self.padding_func = right_padding if padding_side == 'right' else left_padding
        self.processor = processor
        self.padding_side = padding_side

    def __call__(self, samples: list[PreferenceSample]) -> PreferenceBatch:
        return_dict = {'meta_info': {}}
        current_device = get_current_device()

        if os.environ.get('MULTI_IMAGES_INFERENCE_MODELS') == 'Yes':
            images = [[sample['image']] for sample in samples] * 2
        else:
            images = [sample['image'] for sample in samples] * 2

        # FIXME: special for gemma3 processor, will be merge in next version
        if isinstance(self.processor, transformers.Gemma3Processor):
            images = [[convert_to_rgb(sample['image'])] for sample in samples] * 2
            return_dict['meta_info']['images'] = images
        else:
            return_dict['meta_info']['images'] = images

        concated_text = [sample['better_conversation'] for sample in samples] + [
            sample['worse_conversation'] for sample in samples
        ]

        multi_modal_padding = self.processor(
            images=images,
            text=concated_text,
            return_tensors='pt',
            padding=True,
            padding_side=self.padding_side,
            return_attention_mask=True,
        )

        return_dict.update(multi_modal_padding)

        for key, value in return_dict.items():
            if isinstance(value, torch.Tensor):
                return_dict[key] = value.to(current_device)
            elif key == 'pixel_values':

                def move_to_device(item):
                    if isinstance(item, list):
                        return [move_to_device(sub_item) for sub_item in item]
                    elif isinstance(item, torch.Tensor):
                        return item.to(current_device)
                    return item

                return_dict[key] = move_to_device(value)

        better_response_lens = [sample['better_response_lens'] for sample in samples]
        worse_response_lens = [sample['worse_response_lens'] for sample in samples]
        return_dict['meta_info']['response_lens'] = better_response_lens + worse_response_lens
        return return_dict


class SafetyPreferenceDataset(Dataset):

    def __init__(
        self,
        path: str,
        template: str,
        tokenizer: transformers.PreTrainedTokenizer,
        processor: transformers.ProcessorMixin | transforms.Compose | None = None,
        name: str | None = None,
        size: int | None = None,
        split: str | None = None,
        data_files: str | None = None,
        optional_args: list | str = [],
    ):
        super().__init__()
        assert path, f'You must set the valid datasets path! Here is {path}'
        assert template, f'You must set the valid template path! Here is {template}'
        self.tokenizer = tokenizer
        self.processor = processor
        self.template = template

        if isinstance(optional_args, str):
            optional_args = [optional_args]

        self.raw_data = load_dataset(
            path,
            name=name if name and name != 'None' else None,
            split=split if split and split != 'None' else None,
            data_files=data_files if data_files and data_files != 'None' else None,
            *optional_args,
            trust_remote_code=True,
        )

        if size:
            size = min(size, len(self.raw_data))
            self.raw_data = self.raw_data.select(range(int(size)))

        self.valid_indices = self.filter_indices()

    def filter_indices(self):
        valid_indices = []
        for i, item in tqdm(
            enumerate(self.raw_data),
            disable=not is_main_process(),
            total=len(self.raw_data),
            desc='Filtering valid indices',
        ):
            if not hasattr(self.template, 'check_equal'):
                valid_indices.append(i)
                continue
            if not self.template.check_equal(item):
                if hasattr(self.template, 'check_validation'):
                    if not self.template.check_validation(item):
                        continue
                valid_indices.append(i)
        return valid_indices

    def preprocess(self, raw_sample: dict[str, Any]) -> SafetyPreferenceSample:
        better_conversation, worse_conversation, meta_info = self.template.format_preference_sample(
            raw_sample
        )
        better_conversation = (
            better_conversation + self.tokenizer.eos_token
            if better_conversation[-1] != self.tokenizer.eos_token
            else better_conversation
        )
        worse_conversation = (
            worse_conversation + self.tokenizer.eos_token
            if worse_conversation[-1] != self.tokenizer.eos_token
            else worse_conversation
        )
        return_dict = {}
        return_dict['better_response_lens'] = len(
            self.tokenize(meta_info['better_response'], {}, add_special_tokens=False)['input_ids'][
                0
            ]
        )
        return_dict['worse_response_lens'] = len(
            self.tokenize(meta_info['worse_response'], {}, add_special_tokens=False)['input_ids'][0]
        )
        return_dict['better_conversation'] = better_conversation
        return_dict['worse_conversation'] = worse_conversation
        return_dict['is_better_safe'] = meta_info['is_better_safe']
        return_dict['is_worse_safe'] = meta_info['is_worse_safe']
        return_dict['image'] = meta_info['image']

        return return_dict

    def get_collator(self) -> Callable[[list[dict[str, torch.Tensor]]], dict[str, torch.Tensor]]:
        return SafetyPreferenceCollator(
            self.tokenizer.pad_token_id, self.processor, self.tokenizer.padding_side
        )

    def tokenize(
        self,
        conversation: str,
        meta_info: dict[str, Any],
        add_special_tokens: bool = True,
        padding: bool | str | PaddingStrategy = PaddingStrategy.DO_NOT_PAD,
        truncation: bool | str | TruncationStrategy = TruncationStrategy.LONGEST_FIRST,
        max_length: int | None = None,
    ) -> torch.LongTensor:  # size = (L,)
        """Tokenize a text string into a tensor representation."""
        if max_length is None:
            max_length = self.tokenizer.model_max_length

        return self.processor(
            text=conversation,
            images=meta_info.get('image', None),
            add_special_tokens=add_special_tokens,
            padding=padding,
            max_length=max_length,
            truncation=truncation,
            return_tensors='pt',
        )

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        """Get a tokenized data sample by index."""
        raw_sample = self.raw_data[self.valid_indices[index]]
        data = self.preprocess(raw_sample)
        return data

    def __len__(self) -> int:
        """Get the number of samples in the dataset."""
        return len(self.valid_indices)


class SafetyPreferenceCollator:

    def __init__(
        self,
        pad_token_id: int,
        processor: transformers.ProcessorMixin | transforms.Compose | None = None,
        padding_side: str = 'right',
    ) -> None:
        """Initialize a collator."""
        self.pad_token_id = pad_token_id
        self.padding_func = right_padding if padding_side == 'right' else left_padding
        self.processor = processor
        self.padding_side = padding_side

    def __call__(self, samples: list[SafetyPreferenceSample]) -> SafetyPreferenceBatch:
        return_dict = {'meta_info': {}}
        current_device = get_current_device()

        images = [sample['image'] for sample in samples] * 2
        return_dict['meta_info']['images'] = images
        concated_text = [sample['better_conversation'] for sample in samples] + [
            sample['worse_conversation'] for sample in samples
        ]

        multi_modal_padding = self.processor(
            images=images,
            text=concated_text,
            return_tensors='pt',
            padding=True,
            padding_side=self.padding_side,
            return_attention_mask=True,
        )

        for key, value in multi_modal_padding.items():
            if isinstance(value, torch.Tensor):
                return_dict[key] = value.to(current_device)

        better_response_lens = [sample['better_response_lens'] for sample in samples]
        worse_response_lens = [sample['worse_response_lens'] for sample in samples]
        return_dict['meta_info']['is_better_safe'] = [
            sample['is_better_safe'] for sample in samples
        ]
        return_dict['meta_info']['is_worse_safe'] = [sample['is_worse_safe'] for sample in samples]
        return_dict['meta_info']['response_lens'] = better_response_lens + worse_response_lens
        return return_dict
