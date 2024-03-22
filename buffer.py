import torch
import datasets
from transformer_lens import HookedTransformer
from math import ceil


class ActivationsBufferConfig:
    def __init__(
            self,
            model_name,
            layers,
            dataset_name,
            act_site="mlp.hook_pre",
            dataset_split=None,
            buffer_size=256,
            min_capacity=128,
            model_batch_size=8,
            samples_per_seq=16,
            act_size=None,
            seed=None,
            device="cuda",
            dtype=torch.bfloat16,
    ):
        """
        :param model_name: the hf model name
        :param layers: which layers to get activations from, passed as a list of ints
        :param dataset_name: the name of the hf dataset to use
        :param act_site: the tl key to get activations from
        :param dataset_split: the split of the dataset to use
        :param buffer_size: the size of the buffer, in number of activations
        :param min_capacity: the minimum guaranteed capacity of the buffer, in number of activations, used to determine
        when to refresh the buffer
        :param model_batch_size: the batch size to use in the model when generating activations
        :param samples_per_seq: the number of activations to randomly sample from each sequence
        :param act_size: the size of the activations vectors. If None, it will use the size provided by the model's cfg
        :param seed: the seed to use for dataset shuffling and activation sampling
        :param device: the device to use for the buffer and model
        :param dtype: the dtype to use for the buffer and model
        """

        assert isinstance(layers, list) and len(layers) > 0, "layers must be a non-empty list of ints"

        self.model_name = model_name
        self.layers = layers
        self.dataset_name = dataset_name
        self.act_names = [f"blocks.{layer}.{act_site}" for layer in layers]  # the tl keys to grab activations from todo
        self.dataset_split = dataset_split
        self.buffer_size = buffer_size
        self.min_capacity = min_capacity
        self.model_batch_size = model_batch_size
        self.samples_per_seq = samples_per_seq
        self.act_size = act_size
        self.seed = seed
        self.device = device
        self.dtype = dtype
        self.final_layer = max(layers)  # the final layer that needs to be run


class ActivationsBuffer:
    """
    A data buffer to store MLP activations for training the autoencoder.

    Adapted from code by Neel Nanda: https://github.com/neelnanda-io/1L-Sparse-Autoencoder/blob/main/utils.py

    Cleaned up and modified to move all state inside the class, including the model reference, in order to simplify
    composition. The original design which reloads the buffer once it dips below 1/2 of capacity remains, although I'm
    unclear why this is done, probably just a hack to avoid having to worry about overflows
    """

    def __init__(self, cfg: ActivationsBufferConfig, hf_model=None):
        self.cfg = cfg

        if cfg.seed:
            torch.manual_seed(cfg.seed)

        # pointer to the current position in the dataset
        self.dataset_pointer = 0

        # load, shuffle, and flatten the dataset
        self.dataset = datasets.load_dataset(cfg.dataset_name, split=cfg.dataset_split).shuffle(seed=cfg.seed).flatten_indices()

        # load the model into a HookedTransformer
        self.model = HookedTransformer.from_pretrained_no_processing(
            model_name=cfg.model_name,
            hf_model=hf_model,
            device=cfg.device,
            dtype=cfg.dtype
        )

        # if the act_size is not provided, use the size from the model's cfg
        if cfg.act_size is None:
            self.cfg.act_size = self.model.cfg.d_mlp

        # the buffer to store activations in, with shape (buffer_size, len(layers), act_size)
        self.buffer = torch.zeros((cfg.buffer_size, len(self.cfg.layers), cfg.act_size), dtype=cfg.dtype).to(cfg.device)

        # pointer to read/write location in the buffer, reset to 0 after refresh is called
        # starts at buffer_size to be fully filled on first refresh
        self.buffer_pointer = self.cfg.buffer_size

        # initial buffer fill
        self.refresh()

    @torch.no_grad()
    def refresh(self):
        """
        Whenever the buffer is refreshed, we remove the first `buffer_pointer` activations that were used, shift the
        remaining activations to the start of the buffer, and then fill the rest of the buffer with `buffer_pointer` new
        activations from the model.
        """

        # shift the remaining activations to the start of the buffer
        self.buffer = torch.roll(self.buffer, -self.buffer_pointer, 0)

        # fill the rest of the buffer with `buffer_pointer` new activations from the model
        while self.buffer_pointer > 0:
            # if we have less than a full `model_batch_size` left to get, use the remaining sequences
            batch_size = min(ceil(self.buffer_pointer/self.cfg.samples_per_seq), self.cfg.model_batch_size)

            # get the next batch of text from the dataset (batch_size, seq_len)
            seqs = self.dataset['text'][self.dataset_pointer:self.dataset_pointer + batch_size]

            # update the dataset pointer
            self.dataset_pointer = (self.dataset_pointer + batch_size) % len(self.dataset['text'])

            # run the seqs through the model to get the activations
            out, cache = self.model.run_with_cache(seqs, stop_at_layer=self.cfg.final_layer+1,
                                                   names_filter=self.cfg.act_names)

            # clean up logits in order to free the graph memory
            del out
            torch.cuda.empty_cache()

            # store the activations in the buffer
            acts = torch.stack([cache[name] for name in self.cfg.act_names], dim=-2)
            # (batch, pos, layers, act_size) -> (batch*samples_per_seq, layers, act_size)
            acts = acts[:, torch.randperm(acts.shape[-3])[:self.cfg.samples_per_seq]].flatten(0, 1)
            write_pointer = self.cfg.buffer_size - self.buffer_pointer

            new_acts = min(acts.shape[0], self.buffer_pointer)  # the number of acts to write, capped by buffer_pointer
            self.buffer[write_pointer:write_pointer + acts.shape[0]] = acts[:new_acts]

            # update the buffer pointer by the number of activations we just added
            self.buffer_pointer -= new_acts

        assert self.buffer_pointer == 0, "Buffer pointer should be 0 after refresh"

    @torch.no_grad()
    def next(self, batch: int = None):
        if self.cfg.buffer_size - (self.buffer_pointer + (batch or 1)) < self.cfg.min_capacity:
            print("Refreshing the buffer!")
            self.refresh()

        if batch is None:
            out = self.buffer[self.buffer_pointer]
        else:
            out = self.buffer[self.buffer_pointer:self.buffer_pointer + batch]

        self.buffer_pointer += batch or 1

        return out