import importlib
import logging
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple, Union

import torch
from PIL import Image
from transformers import BatchEncoding, BatchFeature, ProcessorMixin

try:
    from fast_plaid import search
except ImportError:
    logging.info(
        "FastPlaid is not installed.If you want to use it:Instal with `pip install --no-deps fast-plaid fastkmeans`"
    )

from colpali_engine.utils.torch_utils import get_torch_device


class BaseVisualRetrieverProcessor(ABC, ProcessorMixin):
    """
    Base class for visual retriever processors.
    """

    @abstractmethod
    def process_images(
        self,
        images: List[Image.Image],
    ) -> Union[BatchFeature, BatchEncoding]:
        pass

    @abstractmethod
    def process_queries(
        self,
        queries: List[str],
        max_length: int = 50,
        suffix: Optional[str] = None,
    ) -> Union[BatchFeature, BatchEncoding]:
        pass

    @abstractmethod
    def score(
        self,
        qs: List[torch.Tensor],
        ps: List[torch.Tensor],
        device: Optional[Union[str, torch.device]] = None,
        **kwargs,
    ) -> torch.Tensor:
        pass

    @staticmethod
    def score_single_vector(
        qs: List[torch.Tensor],
        ps: List[torch.Tensor],
        device: Optional[Union[str, torch.device]] = None,
    ) -> torch.Tensor:
        """
        Compute the dot product score for the given single-vector query and passage embeddings.
        """
        device = device or get_torch_device("auto")

        if len(qs) == 0:
            raise ValueError("No queries provided")
        if len(ps) == 0:
            raise ValueError("No passages provided")

        qs_stacked = torch.stack(qs).to(device)
        ps_stacked = torch.stack(ps).to(device)

        scores = torch.einsum("bd,cd->bc", qs_stacked, ps_stacked)
        assert scores.shape[0] == len(qs), f"Expected {len(qs)} scores, got {scores.shape[0]}"

        scores = scores.to(torch.float32)
        return scores

### MHU
#  Example: one query has 8 tokens, one image has 1024 tokens, find out the most similar patch for each query token within 1024 tokens
# (1, 1, 8) - best patch match for each of the 8 query tokens
    @staticmethod
    def score_multi_vector(
        qs: Union[torch.Tensor, List[torch.Tensor]],
        ps: Union[torch.Tensor, List[torch.Tensor]],
        batch_size: int = 128,
        device: Optional[Union[str, torch.device]] = None,
    ) -> torch.Tensor:
        """
        Compute the late-interaction/MaxSim score (ColBERT-like) for the given multi-vector
        query embeddings (`qs`) and passage embeddings (`ps`). For ColPali, a passage is the
        image of a document page.

        Because the embedding tensors are multi-vector and can thus have different shapes, they
        should be fed as:
        (1) a list of tensors, where the i-th tensor is of shape (sequence_length_i, embedding_dim)
        (2) a single tensor of shape (n_passages, max_sequence_length, embedding_dim) -> usually
            obtained by padding the list of tensors.

        Args:
            qs (`Union[torch.Tensor, List[torch.Tensor]`): Query embeddings.
            ps (`Union[torch.Tensor, List[torch.Tensor]`): Passage embeddings.
            batch_size (`int`, *optional*, defaults to 128): Batch size for computing scores.
            device (`Union[str, torch.device]`, *optional*): Device to use for computation. If not
                provided, uses `get_torch_device("auto")`.

        Returns:
            `torch.Tensor`: A tensor of shape `(n_queries, n_passages)` containing the scores. The score
            tensor is saved on the "cpu" device.
        """
        device = device or get_torch_device("auto")

        if len(qs) == 0:
            raise ValueError("No queries provided")
        if len(ps) == 0:
            raise ValueError("No passages provided")

        scores_list: List[torch.Tensor] = []

        for i in range(0, len(qs), batch_size):
            scores_batch = []
            qs_batch = torch.nn.utils.rnn.pad_sequence(qs[i : i + batch_size], batch_first=True, padding_value=0).to(
                device
            )
            for j in range(0, len(ps), batch_size):
                ps_batch = torch.nn.utils.rnn.pad_sequence(
                    ps[j : j + batch_size], batch_first=True, padding_value=0
                ).to(device)
### MHU
### The similarity is calculated using this line: the MaxSim operation (ColBERT style): For each query token, find the maximum similarity across all passage tokens.
### This computes the dot product between every query token embedding and every passage token embedding for all query-passage pairs in the batch
                scores_batch.append(torch.einsum("bnd,csd->bcns", qs_batch, ps_batch).max(dim=3)[0].sum(dim=2))
            scores_batch = torch.cat(scores_batch, dim=1).cpu()
            scores_list.append(scores_batch)

        scores = torch.cat(scores_list, dim=0)
        assert scores.shape[0] == len(qs), f"Expected {len(qs)} scores, got {scores.shape[0]}"

        scores = scores.to(torch.float32)
        return scores

    @staticmethod
    def get_topk_plaid(
        qs: Union[torch.Tensor, List[torch.Tensor]],
        plaid_index: "search.FastPlaid",
        k: int = 10,
        batch_size: int = 128,
        device: Optional[Union[str, torch.device]] = None,
    ) -> torch.Tensor:
        """
        Experimental: Compute the late-interaction/MaxSim score (ColBERT-like) for the given multi-vector
        query embeddings (`qs`) and passage embeddings endoded in a plaid index. For ColPali, a passage is the
        image of a document page.
        """
        device = device or get_torch_device("auto")

        if len(qs) == 0:
            raise ValueError("No queries provided")

        scores_list: List[torch.Tensor] = []

        for i in range(0, len(qs), batch_size):
            scores_batch = []
            qs_batch = torch.nn.utils.rnn.pad_sequence(qs[i : i + batch_size], batch_first=True, padding_value=0).to(
                device
            )
            # Use the plaid index to get the top-k scores
            scores_batch = plaid_index.search(
                queries_embeddings=qs_batch.to(torch.float32),
                top_k=k,
            )
            scores_list.append(scores_batch)

        return scores_list

    @staticmethod
    def create_plaid_index(
        ps: Union[torch.Tensor, List[torch.Tensor]],
        device: Optional[Union[str, torch.device]] = None,
    ) -> torch.Tensor:
        """
        Experimental: Create a FastPlaid index from the given passage embeddings.
        Args:
            ps (`Union[torch.Tensor, List[torch.Tensor]]`): Passage embeddings. Should be a list of tensors,
                where each tensor is of shape (sequence_length_i, embedding_dim).
            device (`Optional[Union[str, torch.device]]`, *optional*): Device to use for computation. If not
                provided, uses `get_torch_device("auto")`.
        """
        # assert fast_plaid is installed
        if not importlib.util.find_spec("fast_plaid"):
            raise ImportError("FastPlaid is not installed. Please install it with `pip install fast-plaid`.")

        fast_plaid_index = search.FastPlaid(index="index")
        # torch.nn.utils.rnn.pad_sequence(ds, batch_first=True, padding_value=0).to(device)
        device = device or get_torch_device("auto")
        fast_plaid_index.create(documents_embeddings=[d.to(device).to(torch.float32) for d in ps])
        return fast_plaid_index

    @abstractmethod
    def get_n_patches(
        self,
        image_size: Tuple[int, int],
        *args,
        **kwargs,
    ) -> Tuple[int, int]:
        """
        Get the number of patches (n_patches_x, n_patches_y) that will be used to process an
        image of size (height, width) with the given patch size.
        """
        pass
