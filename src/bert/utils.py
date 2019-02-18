import logging as log

from typing import Dict

import torch
import torch.nn as nn

from ..preprocess import parse_task_list_arg

from allennlp.modules import scalar_mix

from .custom_bert_model import BertModelForProbing

class BertEmbedderModule(nn.Module):
    """ Wrapper for BERT module to fit into jiant APIs. """

    def __init__(self, args, cache_dir=None):
        super(BertEmbedderModule, self).__init__()

        self.model = BertModelForProbing.from_pretrained(
                args.bert_model_name,
                cache_dir=cache_dir)
        self.embeddings_mode = args.bert_embeddings_mode
        self.num_layers = self.model.config.num_hidden_layers
        if int(args.bert_max_layer) >= 0:
            self.max_layer = int(args.bert_max_layer)
        else:
            self.max_layer = self.num_layers
        assert self.max_layer <= self.num_layers

        # Set trainability of this module.
        for param in self.model.parameters():
            param.requires_grad = bool(args.bert_fine_tune)

        # Configure scalar mixing, ELMo-style.
        if self.embeddings_mode == "mix":
            if not args.bert_fine_tune:
                log.warning("NOTE: bert_embeddings_mode='mix', so scalar "
                            "mixing weights will be fine-tuned even if BERT "
                            "model is frozen.")
            # TODO: if doing multiple target tasks, allow for multiple sets of
            # scalars. See the ELMo implementation here:
            # https://github.com/allenai/allennlp/blob/master/allennlp/modules/elmo.py#L115
            assert len(parse_task_list_arg(args.target_tasks)) <= 1, \
                    ("bert_embeddings_mode='mix' only supports a single set of "
                     "scalars (but if you need this feature, see the TODO in "
                     "the code!)")
            # Always have one more mixing weight, for lexical layer.
            self.scalar_mix = scalar_mix.ScalarMix(self.max_layer + 1,
                                                   do_layer_norm=False)


    def forward(self, sent: Dict[str, torch.LongTensor],
                unused_task_name: str="") -> torch.FloatTensor:
        """ Run BERT to get hidden states.

        Args:
            sent: batch dictionary

        Returns:
            h: [batch_size, seq_len, d_emb]
        """
        assert "bert_wpm_pretokenized" in sent
        # <int32> [batch_size, var_seq_len]
        var_ids = sent["bert_wpm_pretokenized"]
        # BERT supports up to 512 tokens; see section 3.2 of https://arxiv.org/pdf/1810.04805.pdf
        assert var_ids.size()[1] <= 512
        ids = var_ids

        mask = (ids != 0)
        # "Correct" ids to account for different indexing between BERT and
        # AllenNLP.
        # The AllenNLP indexer adds a '@@UNKNOWN@@' token to the
        # beginning of the vocabulary, *and* treats that as index 1 (index 0 is
        # reserved for padding).
        FILL_ID = 0  # [PAD] for BERT models.
        ids[ids == 0] = FILL_ID + 2
        # Index 1 should never be used since the BERT WPM uses its own
        # unk token, and handles this at the string level before indexing.
        assert (ids > 1).all()
        ids -= 2

        # short-cut for lexical mode: only run embeddings layer.
        max_layer = self.max_layer if self.embeddings_mode != "only" else 0
        # encoded_layers is a list of layer activations, each of which is
        # <float32> [batch_size, seq_len, output_dim]
        encoded_layers, _ = self.model(ids, token_type_ids=torch.zeros_like(ids),
                                       attention_mask=mask,
                                       output_all_encoded_layers=True,
                                       output_lexical=True,
                                       max_layer=max_layer)
        all_layers = encoded_layers
        assert len(all_layers) == self.max_layer + 1

        if self.embeddings_mode in ["none", "top"]:
            h = all_layers[-1]
        elif self.embeddings_mode == "only":
            h = all_layers[0]
        elif self.embeddings_mode == "cat":
            h = torch.cat([all_layers[-1], all_layers[0]], dim=2)
        elif self.embeddings_mode == "mix":
            h = self.scalar_mix(all_layers, mask=mask)
        else:
            raise NotImplementedError(f"embeddings_mode={self.embeddings_mode}"
                                       " not supported.")

        # <float32> [batch_size, var_seq_len, output_dim]
        return h

    def get_output_dim(self):
        if self.embeddings_mode == "cat":
            return 2*self.model.config.hidden_size
        else:
            return self.model.config.hidden_size


