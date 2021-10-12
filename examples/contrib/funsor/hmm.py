# Copyright (c) 2017-2019 Uber Technologies, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
This example is largely copied from ``examples/hmm.py``.
It illustrates the use of the experimental ``pyro.contrib.funsor`` Pyro backend
through the ``pyroapi`` package, demonstrating the utility of Funsor [0]
as an intermediate representation for probabilistic programs

This example combines Stochastic Variational Inference (SVI) with a
variable elimination algorithm, where we use enumeration to exactly
marginalize out some variables from the ELBO computation. We might
call the resulting algorithm collapsed SVI or collapsed SGVB (i.e
collapsed Stochastic Gradient Variational Bayes). In the case where
we exactly sum out all the latent variables (as is the case here),
this algorithm reduces to a form of gradient-based Maximum
Likelihood Estimation.

To marginalize out discrete variables ``x`` in Pyro's SVI:

1. Verify that the variable dependency structure in your model
    admits tractable inference, i.e. the dependency graph among
    enumerated variables should have narrow treewidth.
2. Annotate each target each such sample site in the model
    with ``infer={"enumerate": "parallel"}``
3. Ensure your model can handle broadcasting of the sample values
    of those variables
4. Use the ``TraceEnum_ELBO`` loss inside Pyro's ``SVI``.

Note that empirical results for the models defined here can be found in
reference [1]. This paper also includes a description of the "tensor
variable elimination" algorithm that Pyro uses under the hood to
marginalize out discrete latent variables.

References

0. "Functional Tensors for Probabilistic Programming",
Fritz Obermeyer, Eli Bingham, Martin Jankowiak,
Du Phan, Jonathan P Chen. https://arxiv.org/abs/1910.10775

1. "Tensor Variable Elimination for Plated Factor Graphs",
Fritz Obermeyer, Eli Bingham, Martin Jankowiak, Justin Chiu,
Neeraj Pradhan, Alexander Rush, Noah Goodman. https://arxiv.org/abs/1902.03210
"""
import argparse
import functools
import logging
import sys

import torch
import torch.nn as nn
from torch.distributions import constraints

from pyro.contrib.examples import polyphonic_data_loader as poly
from pyro.infer.autoguide import AutoDelta
from pyro.ops.indexing import Vindex
from pyro.util import ignore_jit_warnings

try:
    import pyro.contrib.funsor
except ImportError:
    pass

from pyroapi import distributions as dist
from pyroapi import handlers, infer, optim, pyro, pyro_backend

logging.basicConfig(format="%(relativeCreated) 9d %(message)s", level=logging.DEBUG)

# Add another handler for logging debugging events (e.g. for profiling)
# in a separate stream that can be captured.
log = logging.getLogger()
debug_handler = logging.StreamHandler(sys.stdout)
debug_handler.setLevel(logging.DEBUG)
debug_handler.addFilter(filter=lambda record: record.levelno <= logging.DEBUG)
log.addHandler(debug_handler)


# Let's start with a simple Hidden Markov Model.
#
#     x[t-1] --> x[t] --> x[t+1]
#        |        |         |
#        V        V         V
#     y[t-1]     y[t]     y[t+1]
#
# This model includes a plate for the data_dim = 88 keys on the piano. This
# model has two "style" parameters probs_x and probs_y that we'll draw from a
# prior. The latent state is x, and the observed state is y. We'll drive
# probs_* with the guide, enumerate over x, and condition on y.
#
# Importantly, the dependency structure of the enumerated variables has
# narrow treewidth, therefore admitting efficient inference by message passing.
# Pyro's TraceEnum_ELBO will find an efficient message passing scheme if one
# exists.
def model_0(sequences, lengths, args, batch_size=None, include_prior=True):
    assert not torch._C._get_tracing_state()
    num_sequences, max_length, data_dim = sequences.shape
    with handlers.mask(mask=include_prior):
        # Our prior on transition probabilities will be:
        # stay in the same state with 90% probability; uniformly jump to another
        # state with 10% probability.
        probs_x = pyro.sample(
            "probs_x",
            dist.Dirichlet(0.9 * torch.eye(args.hidden_dim) + 0.1).to_event(1),
        )
        # We put a weak prior on the conditional probability of a tone sounding.
        # We know that on average about 4 of 88 tones are active, so we'll set a
        # rough weak prior of 10% of the notes being active at any one time.
        probs_y = pyro.sample(
            "probs_y",
            dist.Beta(0.1, 0.9).expand([args.hidden_dim, data_dim]).to_event(2),
        )
    # In this first model we'll sequentially iterate over sequences in a
    # minibatch; this will make it easy to reason about tensor shapes.
    tones_plate = pyro.plate("tones", data_dim, dim=-1)
    for i in pyro.plate("sequences", len(sequences), batch_size):
        length = lengths[i]
        sequence = sequences[i, :length]
        x = 0
        for t in pyro.markov(range(length)):
            # On the next line, we'll overwrite the value of x with an updated
            # value. If we wanted to record all x values, we could instead
            # write x[t] = pyro.sample(...x[t-1]...).
            x = pyro.sample(
                "x_{}_{}".format(i, t),
                dist.Categorical(probs_x[x]),
                infer={"enumerate": "parallel"},
            )
            with tones_plate:
                pyro.sample(
                    "y_{}_{}".format(i, t),
                    dist.Bernoulli(probs_y[x.squeeze(-1)]),
                    obs=sequence[t],
                )


# To see how enumeration changes the shapes of these sample sites, we can use
# the Trace.format_shapes() to print shapes at each site:
# $ python examples/hmm.py -m 0 -n 1 -b 1 -t 5 --print-shapes
# ...
#  Sample Sites:
#   probs_x dist          | 16 16
#          value          | 16 16
#   probs_y dist          | 16 88
#          value          | 16 88
#     tones dist          |
#          value       88 |
# sequences dist          |
#          value        1 |
#   x_178_0 dist          |
#          value    16  1 |
#   y_178_0 dist    16 88 |
#          value       88 |
#   x_178_1 dist    16  1 |
#          value 16  1  1 |
#   y_178_1 dist 16  1 88 |
#          value       88 |
#   x_178_2 dist 16  1  1 |
#          value    16  1 |
#   y_178_2 dist    16 88 |
#          value       88 |
#   x_178_3 dist    16  1 |
#          value 16  1  1 |
#   y_178_3 dist 16  1 88 |
#          value       88 |
#   x_178_4 dist 16  1  1 |
#          value    16  1 |
#   y_178_4 dist    16 88 |
#          value       88 |
#
# Notice that enumeration (over 16 states) alternates between two dimensions:
# -2 and -3.  If we had not used pyro.markov above, each enumerated variable
# would need its own enumeration dimension.


# Next let's make our simple model faster in two ways: first we'll support
# vectorized minibatches of data, and second we'll support the PyTorch jit
# compiler.  To add batch support, we'll introduce a second plate "sequences"
# and randomly subsample data to size batch_size.  To add jit support we
# silence some warnings and try to avoid dynamic program structure.

# Note that this is the "HMM" model in reference [1] (with the difference that
# in [1] the probabilities probs_x and probs_y are not MAP-regularized with
# Dirichlet and Beta distributions for any of the models)
def model_1(sequences, lengths, args, batch_size=None, include_prior=True):
    # Sometimes it is safe to ignore jit warnings. Here we use the
    # pyro.util.ignore_jit_warnings context manager to silence warnings about
    # conversion to integer, since we know all three numbers will be the same
    # across all invocations to the model.
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = map(int, sequences.shape)
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length
    with handlers.mask(mask=include_prior):
        probs_x = pyro.sample(
            "probs_x",
            dist.Dirichlet(0.9 * torch.eye(args.hidden_dim) + 0.1).to_event(1),
        )
        probs_y = pyro.sample(
            "probs_y",
            dist.Beta(0.1, 0.9).expand([args.hidden_dim, data_dim]).to_event(2),
        )
    tones_plate = pyro.plate("tones", data_dim, dim=-1)
    # We subsample batch_size items out of num_sequences items. Note that since
    # we're using dim=-1 for the notes plate, we need to batch over a different
    # dimension, here dim=-2.
    with pyro.plate("sequences", num_sequences, batch_size, dim=-2) as batch:
        lengths = lengths[batch]
        x = 0
        # If we are not using the jit, then we can vary the program structure
        # each call by running for a dynamically determined number of time
        # steps, lengths.max(). However if we are using the jit, then we try to
        # keep a single program structure for all minibatches; the fixed
        # structure ends up being faster since each program structure would
        # need to trigger a new jit compile stage.
        for t in pyro.markov(range(max_length if args.jit else lengths.max())):
            with handlers.mask(mask=(t < lengths).unsqueeze(-1)):
                x = pyro.sample(
                    "x_{}".format(t),
                    dist.Categorical(probs_x[x]),
                    infer={"enumerate": "parallel"},
                )
                with tones_plate:
                    pyro.sample(
                        "y_{}".format(t),
                        dist.Bernoulli(probs_y[x.squeeze(-1)]),
                        obs=sequences[batch, t],
                    )


# Let's see how batching changes the shapes of sample sites:
# $ python examples/hmm.py -m 1 -n 1 -t 5 --batch-size=10 --print-shapes
# ...
#  Sample Sites:
#   probs_x dist             | 16 16
#          value             | 16 16
#   probs_y dist             | 16 88
#          value             | 16 88
#     tones dist             |
#          value          88 |
# sequences dist             |
#          value          10 |
#       x_0 dist       10  1 |
#          value    16  1  1 |
#       y_0 dist    16 10 88 |
#          value       10 88 |
#       x_1 dist    16 10  1 |
#          value 16  1  1  1 |
#       y_1 dist 16  1 10 88 |
#          value       10 88 |
#       x_2 dist 16  1 10  1 |
#          value    16  1  1 |
#       y_2 dist    16 10 88 |
#          value       10 88 |
#       x_3 dist    16 10  1 |
#          value 16  1  1  1 |
#       y_3 dist 16  1 10 88 |
#          value       10 88 |
#       x_4 dist 16  1 10  1 |
#          value    16  1  1 |
#       y_4 dist    16 10 88 |
#          value       10 88 |
#
# Notice that we're now using dim=-2 as a batch dimension (of size 10),
# and that the enumeration dimensions are now dims -3 and -4.


# Next let's add a dependency of y[t] on y[t-1].
#
#     x[t-1] --> x[t] --> x[t+1]
#        |        |         |
#        V        V         V
#     y[t-1] --> y[t] --> y[t+1]
#
# Note that this is the "arHMM" model in reference [1].
def model_2(sequences, lengths, args, batch_size=None, include_prior=True):
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = map(int, sequences.shape)
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length
    with handlers.mask(mask=include_prior):
        probs_x = pyro.sample(
            "probs_x",
            dist.Dirichlet(0.9 * torch.eye(args.hidden_dim) + 0.1).to_event(1),
        )
        probs_y = pyro.sample(
            "probs_y",
            dist.Beta(0.1, 0.9).expand([args.hidden_dim, 2, data_dim]).to_event(3),
        )
    tones_plate = pyro.plate("tones", data_dim, dim=-1)
    with pyro.plate("sequences", num_sequences, batch_size, dim=-2) as batch:
        lengths = lengths[batch]
        x, y = 0, 0
        for t in pyro.markov(range(max_length if args.jit else lengths.max())):
            with handlers.mask(mask=(t < lengths).unsqueeze(-1)):
                x = pyro.sample(
                    "x_{}".format(t),
                    dist.Categorical(probs_x[x]),
                    infer={"enumerate": "parallel"},
                )
                # Note the broadcasting tricks here: to index probs_y on tensors x and y,
                # we also need a final tensor for the tones dimension. This is conveniently
                # provided by the plate associated with that dimension.
                with tones_plate as tones:
                    y = pyro.sample(
                        "y_{}".format(t),
                        dist.Bernoulli(probs_y[x, y, tones]),
                        obs=sequences[batch, t],
                    ).long()


# Next consider a Factorial HMM with two hidden states.
#
#    w[t-1] ----> w[t] ---> w[t+1]
#        \ x[t-1] --\-> x[t] --\-> x[t+1]
#         \  /       \  /       \  /
#          \/         \/         \/
#        y[t-1]      y[t]      y[t+1]
#
# Note that since the joint distribution of each y[t] depends on two variables,
# those two variables become dependent. Therefore during enumeration, the
# entire joint space of these variables w[t],x[t] needs to be enumerated.
# For that reason, we set the dimension of each to the square root of the
# target hidden dimension.
#
# Note that this is the "FHMM" model in reference [1].
def model_3(sequences, lengths, args, batch_size=None, include_prior=True):
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = map(int, sequences.shape)
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length
    hidden_dim = int(args.hidden_dim ** 0.5)  # split between w and x
    with handlers.mask(mask=include_prior):
        probs_w = pyro.sample(
            "probs_w", dist.Dirichlet(0.9 * torch.eye(hidden_dim) + 0.1).to_event(1)
        )
        probs_x = pyro.sample(
            "probs_x", dist.Dirichlet(0.9 * torch.eye(hidden_dim) + 0.1).to_event(1)
        )
        probs_y = pyro.sample(
            "probs_y",
            dist.Beta(0.1, 0.9).expand([hidden_dim, hidden_dim, data_dim]).to_event(3),
        )
    tones_plate = pyro.plate("tones", data_dim, dim=-1)
    with pyro.plate("sequences", num_sequences, batch_size, dim=-2) as batch:
        lengths = lengths[batch]
        w, x = 0, 0
        for t in pyro.markov(range(max_length if args.jit else lengths.max())):
            with handlers.mask(mask=(t < lengths).unsqueeze(-1)):
                w = pyro.sample(
                    "w_{}".format(t),
                    dist.Categorical(probs_w[w]),
                    infer={"enumerate": "parallel"},
                )
                x = pyro.sample(
                    "x_{}".format(t),
                    dist.Categorical(probs_x[x]),
                    infer={"enumerate": "parallel"},
                )
                with tones_plate as tones:
                    pyro.sample(
                        "y_{}".format(t),
                        dist.Bernoulli(probs_y[w, x, tones]),
                        obs=sequences[batch, t],
                    )


# By adding a dependency of x on w, we generalize to a
# Dynamic Bayesian Network.
#
#     w[t-1] ----> w[t] ---> w[t+1]
#        |  \       |  \       |   \
#        | x[t-1] ----> x[t] ----> x[t+1]
#        |   /      |   /      |   /
#        V  /       V  /       V  /
#     y[t-1]       y[t]      y[t+1]
#
# Note that message passing here has roughly the same cost as with the
# Factorial HMM, but this model has more parameters.
#
# Note that this is the "PFHMM" model in reference [1].
def model_4(sequences, lengths, args, batch_size=None, include_prior=True):
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = map(int, sequences.shape)
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length
    hidden_dim = int(args.hidden_dim ** 0.5)  # split between w and x
    with handlers.mask(mask=include_prior):
        probs_w = pyro.sample(
            "probs_w", dist.Dirichlet(0.9 * torch.eye(hidden_dim) + 0.1).to_event(1)
        )
        probs_x = pyro.sample(
            "probs_x",
            dist.Dirichlet(0.9 * torch.eye(hidden_dim) + 0.1)
            .expand_by([hidden_dim])
            .to_event(2),
        )
        probs_y = pyro.sample(
            "probs_y",
            dist.Beta(0.1, 0.9).expand([hidden_dim, hidden_dim, data_dim]).to_event(3),
        )
    tones_plate = pyro.plate("tones", data_dim, dim=-1)
    with pyro.plate("sequences", num_sequences, batch_size, dim=-2) as batch:
        lengths = lengths[batch]
        # Note the broadcasting tricks here: we declare a hidden torch.arange and
        # ensure that w and x are always tensors so we can unsqueeze them below,
        # thus ensuring that the x sample sites have correct distribution shape.
        w = x = torch.tensor(0, dtype=torch.long)
        for t in pyro.markov(range(max_length if args.jit else lengths.max())):
            with handlers.mask(mask=(t < lengths).unsqueeze(-1)):
                w = pyro.sample(
                    "w_{}".format(t),
                    dist.Categorical(probs_w[w]),
                    infer={"enumerate": "parallel"},
                )
                x = pyro.sample(
                    "x_{}".format(t),
                    dist.Categorical(Vindex(probs_x)[w, x]),
                    infer={"enumerate": "parallel"},
                )
                with tones_plate as tones:
                    pyro.sample(
                        "y_{}".format(t),
                        dist.Bernoulli(probs_y[w, x, tones]),
                        obs=sequences[batch, t],
                    )


# Next let's consider a neural HMM model.
#
#     x[t-1] --> x[t] --> x[t+1]   } standard HMM +
#        |        |         |
#        V        V         V
#     y[t-1] --> y[t] --> y[t+1]   } neural likelihood
#
# First let's define a neural net to generate y logits.
class TonesGenerator(nn.Module):
    def __init__(self, args, data_dim):
        self.args = args
        self.data_dim = data_dim
        super().__init__()
        self.x_to_hidden = nn.Linear(args.hidden_dim, args.nn_dim)
        self.y_to_hidden = nn.Linear(args.nn_channels * data_dim, args.nn_dim)
        self.conv = nn.Conv1d(1, args.nn_channels, 3, padding=1)
        self.hidden_to_logits = nn.Linear(args.nn_dim, data_dim)
        self.relu = nn.ReLU()

    def forward(self, x, y):
        # Hidden units depend on two inputs: a one-hot encoded categorical variable x, and
        # a bernoulli variable y. Whereas x will typically be enumerated, y will be observed.
        # We apply x_to_hidden independently from y_to_hidden, then broadcast the non-enumerated
        # y part up to the enumerated x part in the + operation.
        x_onehot = y.new_zeros(x.shape[:-1] + (self.args.hidden_dim,)).scatter_(
            -1, x, 1
        )
        y_conv = self.relu(self.conv(y.reshape(-1, 1, self.data_dim))).reshape(
            y.shape[:-1] + (-1,)
        )
        h = self.relu(self.x_to_hidden(x_onehot) + self.y_to_hidden(y_conv))
        return self.hidden_to_logits(h)


# We will create a single global instance later.
tones_generator = None


# The neural HMM model now uses tones_generator at each time step.
#
# Note that this is the "nnHMM" model in reference [1].
def model_5(sequences, lengths, args, batch_size=None, include_prior=True):
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = map(int, sequences.shape)
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length

    # Initialize a global module instance if needed.
    global tones_generator
    if tones_generator is None:
        tones_generator = TonesGenerator(args, data_dim)
    pyro.module("tones_generator", tones_generator)

    with handlers.mask(mask=include_prior):
        probs_x = pyro.sample(
            "probs_x",
            dist.Dirichlet(0.9 * torch.eye(args.hidden_dim) + 0.1).to_event(1),
        )
    with pyro.plate("sequences", num_sequences, batch_size, dim=-2) as batch:
        lengths = lengths[batch]
        x = 0
        y = torch.zeros(data_dim)
        for t in pyro.markov(range(max_length if args.jit else lengths.max())):
            with handlers.mask(mask=(t < lengths).unsqueeze(-1)):
                x = pyro.sample(
                    "x_{}".format(t),
                    dist.Categorical(probs_x[x]),
                    infer={"enumerate": "parallel"},
                )
                # Note that since each tone depends on all tones at a previous time step
                # the tones at different time steps now need to live in separate plates.
                with pyro.plate("tones_{}".format(t), data_dim, dim=-1):
                    y = pyro.sample(
                        "y_{}".format(t),
                        dist.Bernoulli(logits=tones_generator(x, y)),
                        obs=sequences[batch, t],
                    )


# Next let's consider a second-order HMM model
# in which x[t+1] depends on both x[t] and x[t-1].
#
#                     _______>______
#         _____>_____/______        \
#        /          /       \        \
#     x[t-1] --> x[t] --> x[t+1] --> x[t+2]
#        |        |          |          |
#        V        V          V          V
#     y[t-1]     y[t]     y[t+1]     y[t+2]
#
#  Note that in this model (in contrast to the previous model) we treat
#  the transition and emission probabilities as parameters (so they have no prior).
#
# Note that this is the "2HMM" model in reference [1].
def model_6(sequences, lengths, args, batch_size=None, include_prior=False):
    num_sequences, max_length, data_dim = sequences.shape
    assert lengths.shape == (num_sequences,)
    assert lengths.max() <= max_length
    hidden_dim = args.hidden_dim

    if not args.raftery_parameterization:
        # Explicitly parameterize the full tensor of transition probabilities, which
        # has hidden_dim cubed entries.
        probs_x = pyro.param(
            "probs_x",
            torch.rand(hidden_dim, hidden_dim, hidden_dim),
            constraint=constraints.simplex,
        )
    else:
        # Use the more parsimonious "Raftery" parameterization of
        # the tensor of transition probabilities. See reference:
        # Raftery, A. E. A model for high-order markov chains.
        # Journal of the Royal Statistical Society. 1985.
        probs_x1 = pyro.param(
            "probs_x1",
            torch.rand(hidden_dim, hidden_dim),
            constraint=constraints.simplex,
        )
        probs_x2 = pyro.param(
            "probs_x2",
            torch.rand(hidden_dim, hidden_dim),
            constraint=constraints.simplex,
        )
        mix_lambda = pyro.param(
            "mix_lambda", torch.tensor(0.5), constraint=constraints.unit_interval
        )
        # we use broadcasting to combine two tensors of shape (hidden_dim, hidden_dim) and
        # (hidden_dim, 1, hidden_dim) to obtain a tensor of shape (hidden_dim, hidden_dim, hidden_dim)
        probs_x = mix_lambda * probs_x1 + (1.0 - mix_lambda) * probs_x2.unsqueeze(-2)

    probs_y = pyro.param(
        "probs_y",
        torch.rand(hidden_dim, data_dim),
        constraint=constraints.unit_interval,
    )
    tones_plate = pyro.plate("tones", data_dim, dim=-1)
    with pyro.plate("sequences", num_sequences, batch_size, dim=-2) as batch:
        lengths = lengths[batch]
        x_curr, x_prev = torch.tensor(0), torch.tensor(0)
        # we need to pass the argument `history=2' to `pyro.markov()`
        # since our model is now 2-markov
        for t in pyro.markov(range(lengths.max()), history=2):
            with handlers.mask(mask=(t < lengths).unsqueeze(-1)):
                probs_x_t = Vindex(probs_x)[x_prev, x_curr]
                x_prev, x_curr = x_curr, pyro.sample(
                    "x_{}".format(t),
                    dist.Categorical(probs_x_t),
                    infer={"enumerate": "parallel"},
                )
                with tones_plate:
                    probs_y_t = probs_y[x_curr.squeeze(-1)]
                    pyro.sample(
                        "y_{}".format(t),
                        dist.Bernoulli(probs_y_t),
                        obs=sequences[batch, t],
                    )


# Let's go back to our initial model and make it even faster: we'll support
# vectorized time dimension and use TraceMarkovEnum_ELBO that efficiently eliminates
# vectorized time dimension using the parallel scan algorithm. Note that TraceMarkovEnum_ELBO
# is only supported by funsor backend.
def model_7(sequences, lengths, args, batch_size=None, include_prior=True):
    with ignore_jit_warnings():
        num_sequences, max_length, data_dim = map(int, sequences.shape)
        assert lengths.shape == (num_sequences,)
        assert lengths.max() <= max_length
    with handlers.mask(mask=include_prior):
        probs_x = pyro.sample(
            "probs_x",
            dist.Dirichlet(0.9 * torch.eye(args.hidden_dim) + 0.1).to_event(1),
        )
        probs_y = pyro.sample(
            "probs_y",
            dist.Beta(0.1, 0.9).expand([args.hidden_dim, data_dim]).to_event(2),
        )
    tones_plate = pyro.plate("tones", data_dim, dim=-1)
    # Note that since we're using dim=-2 for the time dimension, we need
    # to batch sequences over a different dimension, here dim=-3.
    with pyro.plate("sequences", num_sequences, batch_size, dim=-3) as batch:
        lengths = lengths[batch]
        batch = batch[:, None]
        x_prev = 0
        # To vectorize time dimension we use pyro.vectorized_markov(name=...).
        # With the help of Vindex and additional unsqueezes we can ensure that
        # dimensions line up properly.
        for t in pyro.vectorized_markov(
            name="time", size=int(max_length if args.jit else lengths.max()), dim=-2
        ):
            with handlers.mask(mask=(t < lengths.unsqueeze(-1)).unsqueeze(-1)):
                x_curr = pyro.sample(
                    "x_{}".format(t),
                    dist.Categorical(probs_x[x_prev]),
                    infer={"enumerate": "parallel"},
                )
                with tones_plate:
                    pyro.sample(
                        "y_{}".format(t),
                        dist.Bernoulli(probs_y[x_curr.squeeze(-1)]),
                        obs=Vindex(sequences)[batch, t],
                    )


# Let's see how vectorizing time dimension changes the shapes of sample sites:
# $ python examples/hmm.py -m 7 --funsor -n 1 --batch-size=10 --print-shapes
# ...
#              Sample Sites:
#               probs_x dist                   | 16 16
#                      value                   | 16 16
#               probs_y dist                   | 16 51
#                      value                   | 16 51
#                 tones dist                   |
#                      value                51 |
#             sequences dist                   |
#                      value                10 |
#                   x_0 dist          10  1  1 |
#                      value       16  1  1  1 |
#                   y_0 dist       16 10  1 51 |
#                      value          10  1 51 |
#  x_slice(0, 71, None) dist          10 71  1 |
#                      value    16  1  1  1  1 |
#  y_slice(0, 71, None) dist    16  1 10 71 51 |
#                      value          10 71 51 |
#  x_slice(1, 72, None) dist          10 71  1 |
#                      value 16  1  1  1  1  1 |
#  y_slice(1, 72, None) dist 16  1  1 10 71 51 |
#                      value          10 71 51 |
#
# Notice that we're now using dim=-2 for the time dimension.
# pyro.vectorized_markov loops three times: first it produces
# t = 0, then vectorized t_prev (torch.arange(0, 71)), and
# finally vectorized t_curr (torch.arange(1, 72)).


models = {
    name[len("model_") :]: model
    for name, model in globals().items()
    if name.startswith("model_")
}


def main(args):
    if args.cuda:
        torch.set_default_tensor_type("torch.cuda.FloatTensor")

    logging.info("Loading data")
    data = poly.load_data(poly.JSB_CHORALES)

    logging.info("-" * 40)
    model = models[args.model]
    logging.info(
        "Training {} on {} sequences".format(
            model.__name__, len(data["train"]["sequences"])
        )
    )
    sequences = data["train"]["sequences"]
    lengths = data["train"]["sequence_lengths"]

    # find all the notes that are present at least once in the training set
    present_notes = (sequences == 1).sum(0).sum(0) > 0
    # remove notes that are never played (we remove 37/88 notes)
    sequences = sequences[..., present_notes]

    if args.truncate:
        lengths = lengths.clamp(max=args.truncate)
        sequences = sequences[:, : args.truncate]
    num_observations = float(lengths.sum())
    pyro.set_rng_seed(args.seed)
    pyro.clear_param_store()

    # We'll train using MAP Baum-Welch, i.e. MAP estimation while marginalizing
    # out the hidden state x. This is accomplished via an automatic guide that
    # learns point estimates of all of our conditional probability tables,
    # named probs_*.
    guide = AutoDelta(
        handlers.block(
            model,
            expose_fn=lambda msg: msg["name"] is not None
            and msg["name"].startswith("probs_"),
        )
    )

    # To help debug our tensor shapes, let's print the shape of each site's
    # distribution, value, and log_prob tensor. Note this information is
    # automatically printed on most errors inside SVI.
    if args.print_shapes:
        if args.model == "0":
            first_available_dim = -2
        elif args.model == "7":
            first_available_dim = -4
        else:
            first_available_dim = -3
        guide_trace = handlers.trace(guide).get_trace(
            sequences, lengths, args=args, batch_size=args.batch_size
        )
        model_trace = handlers.trace(
            handlers.replay(handlers.enum(model, first_available_dim), guide_trace)
        ).get_trace(sequences, lengths, args=args, batch_size=args.batch_size)
        logging.info(model_trace.format_shapes())

    # Bind non-PyTorch parameters to make these functions jittable.
    model = functools.partial(model, args=args)
    guide = functools.partial(guide, args=args)

    # Enumeration requires a TraceEnum elbo and declaring the max_plate_nesting.
    # All of our models have two plates: "data" and "tones".
    optimizer = optim.Adam({"lr": args.learning_rate})
    if args.tmc:
        if args.jit and not args.funsor:
            raise NotImplementedError("jit support not yet added for TraceTMC_ELBO")
        Elbo = infer.JitTraceTMC_ELBO if args.jit else infer.TraceTMC_ELBO
        elbo = Elbo(max_plate_nesting=1 if model is model_0 else 2)
        tmc_model = handlers.infer_config(
            model,
            lambda msg: {"num_samples": args.tmc_num_samples, "expand": False}
            if msg["infer"].get("enumerate", None) == "parallel"
            else {},
        )  # noqa: E501
        svi = infer.SVI(tmc_model, guide, optimizer, elbo)
    else:
        if args.model == "7":
            assert args.funsor
            Elbo = (
                infer.JitTraceMarkovEnum_ELBO
                if args.jit
                else infer.TraceMarkovEnum_ELBO
            )
        else:
            Elbo = infer.JitTraceEnum_ELBO if args.jit else infer.TraceEnum_ELBO
        if args.model == "0":
            max_plate_nesting = 1
        elif args.model == "7":
            max_plate_nesting = 3
        else:
            max_plate_nesting = 2
        elbo = Elbo(
            max_plate_nesting=max_plate_nesting,
            strict_enumeration_warning=True,
            jit_options={"time_compilation": args.time_compilation},
        )
        svi = infer.SVI(model, guide, optimizer, elbo)

    # We'll train on small minibatches.
    logging.info("Step\tLoss")
    for step in range(args.num_steps):
        loss = svi.step(sequences, lengths, batch_size=args.batch_size)
        logging.info("{: >5d}\t{}".format(step, loss / num_observations))

    if args.jit and args.time_compilation:
        logging.debug(
            "time to compile: {} s.".format(elbo._differentiable_loss.compile_time)
        )

    # We evaluate on the entire training dataset,
    # excluding the prior term so our results are comparable across models.
    train_loss = elbo.loss(
        model,
        guide,
        sequences,
        lengths,
        batch_size=sequences.shape[0],
        include_prior=False,
    )
    logging.info("training loss = {}".format(train_loss / num_observations))

    # Finally we evaluate on the test dataset.
    logging.info("-" * 40)
    logging.info(
        "Evaluating on {} test sequences".format(len(data["test"]["sequences"]))
    )
    sequences = data["test"]["sequences"][..., present_notes]
    lengths = data["test"]["sequence_lengths"]
    if args.truncate:
        lengths = lengths.clamp(max=args.truncate)
    num_observations = float(lengths.sum())

    # note that since we removed unseen notes above (to make the problem a bit easier and for
    # numerical stability) this test loss may not be directly comparable to numbers
    # reported on this dataset elsewhere.
    test_loss = elbo.loss(
        model,
        guide,
        sequences,
        lengths,
        batch_size=sequences.shape[0],
        include_prior=False,
    )
    logging.info("test loss = {}".format(test_loss / num_observations))

    # We expect models with higher capacity to perform better,
    # but eventually overfit to the training set.
    capacity = sum(
        value.reshape(-1).size(0) for value in pyro.get_param_store().values()
    )
    logging.info("model_{} capacity = {} parameters".format(args.model, capacity))


if __name__ == "__main__":
    assert pyro.__version__.startswith("1.7.0")
    parser = argparse.ArgumentParser(
        description="MAP Baum-Welch learning Bach Chorales"
    )
    parser.add_argument(
        "-m",
        "--model",
        default="1",
        type=str,
        help="one of: {}".format(", ".join(sorted(models.keys()))),
    )
    parser.add_argument("-n", "--num-steps", default=50, type=int)
    parser.add_argument("-b", "--batch-size", default=8, type=int)
    parser.add_argument("-d", "--hidden-dim", default=16, type=int)
    parser.add_argument("-nn", "--nn-dim", default=48, type=int)
    parser.add_argument("-nc", "--nn-channels", default=2, type=int)
    parser.add_argument("-lr", "--learning-rate", default=0.05, type=float)
    parser.add_argument("-t", "--truncate", type=int)
    parser.add_argument("-p", "--print-shapes", action="store_true")
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--jit", action="store_true")
    parser.add_argument("--time-compilation", action="store_true")
    parser.add_argument("-rp", "--raftery-parameterization", action="store_true")
    parser.add_argument(
        "--tmc",
        action="store_true",
        help="Use Tensor Monte Carlo instead of exact enumeration "
        "to estimate the marginal likelihood. You probably don't want to do this, "
        "except to see that TMC makes Monte Carlo gradient estimation feasible "
        "even with very large numbers of non-reparametrized variables.",
    )
    parser.add_argument("--tmc-num-samples", default=10, type=int)
    parser.add_argument("--funsor", action="store_true")
    args = parser.parse_args()

    if args.funsor:
        import funsor

        funsor.set_backend("torch")
        PYRO_BACKEND = "contrib.funsor"
    else:
        PYRO_BACKEND = "pyro"

    with pyro_backend(PYRO_BACKEND):
        main(args)
