# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import contextlib

import funsor
import torch
from funsor.adjoint import AdjointTape
from funsor.constant import Constant

from pyro.contrib.funsor import to_data, to_funsor
from pyro.contrib.funsor.handlers import enum, plate, provenance, replay, trace
from pyro.distributions.util import copy_docs_from
from pyro.infer import Trace_ELBO as _OrigTrace_ELBO

from .elbo import ELBO, Jit_ELBO


# Work around a bug in unfold_contraction_generic_tuple interacting with
# Approximate introduced in https://github.com/pyro-ppl/funsor/pull/488 .
# Once fixed, this can be replaced by funsor.optimizer.apply_optimizer().
def apply_optimizer(x):
    with funsor.interpretations.normalize:
        expr = funsor.interpreter.reinterpret(x)

    with funsor.optimizer.optimize_base:
        return funsor.interpreter.reinterpret(expr)


def terms_from_trace(tr):
    """Helper function to extract elbo components from execution traces."""
    # data structure containing densities, measures, scales, and identification
    # of free variables as either product (plate) variables or sum (measure) variables
    terms = {
        "log_factors": [],
        "log_measures": [],
        "scale": to_funsor(1.0),
        "plate_vars": frozenset(),
        "measure_vars": frozenset(),
        "plate_to_step": dict(),
    }
    for name, node in tr.nodes.items():
        # add markov dimensions to the plate_to_step dictionary
        if node["type"] == "markov_chain":
            terms["plate_to_step"][node["name"]] = node["value"]
            # ensure previous step variables are added to measure_vars
            for step in node["value"]:
                terms["measure_vars"] |= frozenset(
                    {
                        var
                        for var in step[1:-1]
                        if tr.nodes[var]["funsor"].get("log_measure", None) is not None
                    }
                )
        if (
            node["type"] != "sample"
            or type(node["fn"]).__name__ == "_Subsample"
            or node["infer"].get("_do_not_score", False)
        ):
            continue
        # grab plate dimensions from the cond_indep_stack
        terms["plate_vars"] |= frozenset(
            f.name for f in node["cond_indep_stack"] if f.vectorized
        )
        # grab the log-measure, found only at sites that are not replayed or observed
        if node["funsor"].get("log_measure", None) is not None:
            terms["log_measures"].append(node["funsor"]["log_measure"])
            # sum (measure) variables: the fresh non-plate variables at a site
            terms["measure_vars"] |= (
                frozenset(node["funsor"]["value"].inputs) | {name}
            ) - terms["plate_vars"]
        # grab the scale, assuming a common subsampling scale
        if (
            node.get("replay_active", False)
            and set(node["funsor"]["log_prob"].inputs) & terms["measure_vars"]
            and float(to_data(node["funsor"]["scale"])) != 1.0
        ):
            # model site that depends on enumerated variable: common scale
            terms["scale"] = node["funsor"]["scale"]
        else:  # otherwise: default scale behavior
            node["funsor"]["log_prob"] = (
                node["funsor"]["log_prob"] * node["funsor"]["scale"]
            )
        # grab the log-density, found at all sites except those that are not replayed
        if node["is_observed"] or not node.get("replay_skipped", False):
            terms["log_factors"].append(node["funsor"]["log_prob"])
    # add plate dimensions to the plate_to_step dictionary
    terms["plate_to_step"].update(
        {plate: terms["plate_to_step"].get(plate, {}) for plate in terms["plate_vars"]}
    )
    return terms


@copy_docs_from(_OrigTrace_ELBO)
class Trace_ELBO(ELBO):
    def differentiable_loss(self, model, guide, *args, **kwargs):
        with enum(
            first_available_dim=(-self.max_plate_nesting - 1)
            if self.max_plate_nesting is not None
            and self.max_plate_nesting != float("inf")
            else None
        ), provenance(), plate(
            name="num_particles_vectorized",
            size=self.num_particles,
            dim=-self.max_plate_nesting,
        ) if self.num_particles > 1 else contextlib.ExitStack():
            guide_tr = trace(guide).get_trace(*args, **kwargs)
            model_tr = trace(replay(model, trace=guide_tr)).get_trace(*args, **kwargs)

        model_terms = terms_from_trace(model_tr)
        guide_terms = terms_from_trace(guide_tr)

        plate_vars = (
            guide_terms["plate_vars"] | model_terms["plate_vars"]
        ) - frozenset({"num_particles_vectorized"})

        model_measure_vars = model_terms["measure_vars"] - guide_terms["measure_vars"]
        with funsor.terms.lazy:
            # identify and contract out auxiliary variables in the model with partial_sum_product
            contracted_factors, uncontracted_factors = [], []
            for f in model_terms["log_factors"]:
                if model_measure_vars.intersection(f.inputs):
                    contracted_factors.append(f)
                else:
                    uncontracted_factors.append(f)
            # incorporate the effects of subsampling and handlers.scale through a common scale factor
            contracted_costs = [
                model_terms["scale"] * f
                for f in funsor.sum_product.partial_sum_product(
                    funsor.ops.logaddexp,
                    funsor.ops.add,
                    model_terms["log_measures"] + contracted_factors,
                    plates=plate_vars,
                    eliminate=model_measure_vars,
                )
            ]

        # accumulate costs from model (logp) and guide (-logq)
        costs = contracted_costs + uncontracted_factors  # model costs: logp
        costs += [-f for f in guide_terms["log_factors"]]  # guide costs: -logq

        # compute log_measures corresponding to each cost term
        # the goal is to achieve fine-grained Rao-Blackwellization
        targets = dict()
        for cost in costs:
            if cost.input_vars not in targets:
                targets[cost.input_vars] = Constant(
                    cost.inputs, funsor.Tensor(torch.tensor(0.0))
                )
        with AdjointTape() as tape:
            logzq = funsor.sum_product.sum_product(
                funsor.ops.logaddexp,
                funsor.ops.add,
                guide_terms["log_measures"] + list(targets.values()),
                plates=plate_vars,
                eliminate=(plate_vars | guide_terms["measure_vars"]),
            )
        log_measures = tape.adjoint(
            funsor.ops.logaddexp, funsor.ops.add, logzq, tuple(targets.values())
        )
        with funsor.terms.lazy:
            # finally, integrate out guide variables in the elbo and all plates
            elbo = to_funsor(0, output=funsor.Real)
            for cost in costs:
                target = targets[cost.input_vars]
                log_measure = log_measures[target]
                measure_vars = (frozenset(cost.inputs) - plate_vars) - frozenset(
                    {"num_particles_vectorized"}
                )
                elbo_term = funsor.Integrate(
                    log_measure,
                    cost,
                    measure_vars,
                )
                elbo += elbo_term.reduce(
                    funsor.ops.add, plate_vars & frozenset(cost.inputs)
                )
            # average over Monte-Carlo particles
            elbo = elbo.reduce(funsor.ops.mean)

        return -to_data(apply_optimizer(elbo))


class JitTrace_ELBO(Jit_ELBO, Trace_ELBO):
    pass
