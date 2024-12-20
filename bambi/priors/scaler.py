import numpy as np
import pymc as pm

from bambi.families.univariate import (
    Bernoulli,
    Binomial,
    Cumulative,
    Gaussian,
    StoppingRatio,
    StudentT,
    VonMises,
)
from bambi.model_components import ConstantComponent
from bambi.priors.prior import Prior


class PriorScaler:
    """Scale prior distributions parameters."""

    # Standard deviation multipliefr.
    STD = 2.5

    def __init__(self, model):
        self.model = model
        self.response_component = model.response_component
        self.parent_component = model.components[model.family.likelihood.parent]
        self.has_intercept = self.parent_component.intercept_term is not None
        self.priors = {}

        # Compute mean and std of the response
        if isinstance(self.model.family, (Gaussian, StudentT)):
            self.response_mean = np.mean(self.response_component.term.data)
            self.response_std = np.std(self.response_component.term.data)
        else:
            self.response_mean = 0
            self.response_std = 1

    def get_intercept_stats(self):
        mu = self.response_mean
        sigma = self.STD * self.response_std
        # Only adjust sigma if there is at least one Normal prior for a common term.
        if self.priors:
            sigmas = np.hstack([prior["sigma"] for prior in self.priors.values()])
            x_mean = np.hstack(
                [self.parent_component.terms[term].data.mean(axis=0) for term in self.priors]
            )
            sigma = (sigma**2 + np.dot(sigmas**2, x_mean**2)) ** 0.5

        return mu, sigma

    def get_slope_sigma(self, x):
        return self.STD * (self.response_std / np.std(x))

    def scale_response(self):
        # Here we would add cases for other families if we wanted
        if isinstance(self.model.family, (Gaussian, StudentT)):
            sigma = self.model.components["sigma"]
            if (
                isinstance(sigma, ConstantComponent)
                and hasattr(sigma.prior, "auto_scale")  # not available when `.prior` is a scalar
                and sigma.prior.auto_scale
            ):
                sigma.prior = Prior("HalfStudentT", nu=4, sigma=self.response_std)
        elif isinstance(self.model.family, VonMises):
            kappa = self.model.components["kappa"]
            if (
                isinstance(kappa, ConstantComponent)
                and hasattr(kappa.prior, "auto_scale")  # not available when `.prior` is a scalar
                and kappa.prior.auto_scale
            ):
                kappa.prior = Prior("HalfStudentT", nu=4, sigma=self.response_std)

    def scale_intercept(self, term):
        if term.prior.name != "Normal":
            return
        # Special case for logit/probit links with bernoulli or binomial family
        if isinstance(self.model.family, (Bernoulli, Binomial)) and self.model.family.link[
            "p"
        ].name in ["logit", "probit"]:
            mu = 0
            sigma = 1.5
        else:
            mu, sigma = self.get_intercept_stats()
        term.prior.update(mu=mu, sigma=sigma)

    def scale_common(self, term):
        if term.prior.name != "Normal":
            return

        if term.data.ndim == 1:
            mu = 0
            # Special case for logit/probit links with bernoulli or binomial family
            if isinstance(self.model.family, (Bernoulli, Binomial)) and self.model.family.link[
                "p"
            ].name in ["logit", "probit"]:
                # For interaction terms, distinguish cases where all factor terms are categorical
                if term.kind == "interaction":
                    all_categoric = all(
                        component.kind == "categoric" for component in term.term.components
                    )
                    if all_categoric:
                        sigma = 1
                    else:
                        sigma = 1 / np.std(term.data, axis=0)
                # Single categorical term
                elif term.categorical:
                    sigma = 1
                # Single numerical term
                else:
                    sigma = 1 / np.std(term.data, axis=0)
            # If not, fall back to the regular case
            else:
                sigma = self.get_slope_sigma(term.data)
        # It's a term that spans multiple columns of the design matrix
        else:
            mu = np.zeros(term.data.shape[1])
            sigma = np.zeros(term.data.shape[1])
            # Special case for logit/probit links with bernoulli or binomial family
            if isinstance(self.model.family, (Bernoulli, Binomial)) and self.model.family.link[
                "p"
            ].name in ["logit", "probit"]:
                # Iterate over columns in the data
                for i, value in enumerate(term.data.T):
                    if term.kind == "interaction":
                        # Distinguish cases where all interaction factor terms are categorical
                        all_categoric = all(
                            component.kind == "categoric" for component in term.term.components
                        )
                        if all_categoric:
                            sigma[i] = 1
                        # It's the std dev of the marginal numerical variable (_not_ by group)
                        else:
                            sigma[i] = 1 / np.std(np.sum(term.data, axis=1))
                    # Single categorical term
                    elif term.categorical:
                        sigma[i] = 1
                    # Single numerical term
                    else:
                        sigma[i] = 1 / np.std(term.data, axis=0)
            else:
                for i, value in enumerate(term.data.T):
                    sigma[i] = self.get_slope_sigma(value)

        # Save and set prior
        self.priors.update({term.name: {"mu": mu, "sigma": sigma}})
        term.prior.update(mu=mu, sigma=sigma)

    def scale_group_specific(self, term):
        if term.prior.args["sigma"].name != "HalfNormal":
            return

        # Handle intercepts
        if term.kind == "intercept":
            _, sigma = self.get_intercept_stats()
        # Handle slopes
        else:
            # Recreate the corresponding common effect data
            if len(term.predictor.shape) == 2:
                data_as_common = term.predictor
            else:
                data_as_common = term.predictor[:, None]
            sigma = np.zeros(data_as_common.shape[1])
            for i, value in enumerate(data_as_common.T):
                sigma[i] = self.get_slope_sigma(value)
        term.prior.args["sigma"].update(sigma=np.squeeze(np.atleast_1d(sigma)))

    def scale_threshold(self):
        if isinstance(self.model.family, Cumulative):
            threshold = self.model.components["threshold"]
            if isinstance(threshold, ConstantComponent) and threshold.prior.auto_scale:
                response_level_n = len(np.unique(self.response_component.term.data))
                mu = np.round(np.linspace(-2, 2, num=response_level_n - 1), 2)
                threshold.prior = Prior(
                    "Normal",
                    mu=mu,
                    sigma=1,
                    transform=pm.distributions.transforms.ordered,
                )
        elif isinstance(self.model.family, StoppingRatio):
            threshold = self.model.components["threshold"]
            if isinstance(threshold, ConstantComponent) and threshold.prior.auto_scale:
                response_level_n = len(np.unique(self.response_component.term.data))
                mu = np.zeros(response_level_n - 1)
                threshold.prior = Prior("Normal", mu=mu, sigma=1)

    def scale(self):
        # Scale response
        self.scale_response()

        # Scale common terms
        for term in self.parent_component.common_terms.values():
            if hasattr(term.prior, "auto_scale") and term.prior.auto_scale:
                self.scale_common(term)

        # Scale intercept
        if self.has_intercept:
            term = self.parent_component.intercept_term
            if term.prior.auto_scale:
                self.scale_intercept(term)

        # Scale group-specific terms
        for term in self.parent_component.group_specific_terms.values():
            if term.prior.auto_scale:
                self.scale_group_specific(term)

        # Scale threshold parameters in ordinal families
        self.scale_threshold()
