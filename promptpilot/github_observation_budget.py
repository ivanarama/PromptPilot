"""Successful-observation cost proof, separate from global account counters.

An unknown request retains its entire escrow and poisons the observation. Its
actual server charge is not claimed to be bounded by that accounting escrow.
"""
import copy


class CostUnproved(ValueError):
    pass


class ObservationCost:
    def __init__(self, allowance):
        if type(allowance) is not int or allowance < 0:
            raise ValueError("invalid GraphQL observation allowance")
        self.allowance = allowance
        self.charged = self.observed_actual = self.escrow = 0
        self.uncertain = False

    @property
    def remaining(self):
        return self.allowance - self.charged - self.escrow

    def reserve(self, quote=None):
        if self.uncertain or self.escrow:
            raise CostUnproved("GraphQL observation cost is already uncertain")
        amount = self.remaining if quote is None else quote
        if type(amount) is not int or amount <= 0 or amount > self.remaining:
            raise CostUnproved("GraphQL observation allowance cannot cover another request")
        self.escrow = amount

    def settle(self, actual, *, allow_zero=False):
        if (type(actual) is not int or actual < (0 if allow_zero else 1)
                or not self.escrow or actual > self.escrow):
            if type(actual) is int and actual >= (0 if allow_zero else 1):
                self.observed_actual += actual  # known receipt, even when above escrow
            self.poison()
            raise CostUnproved("GraphQL request cost receipt does not fit its escrow")
        self.charged += actual
        self.observed_actual += actual
        self.escrow = 0

    def bootstrap(self, actual):
        self.reserve()
        self.settle(actual)

    def poison(self):
        self.charged += self.escrow
        self.escrow = 0
        self.uncertain = True


class AvailabilityEnvelope:
    """Never replenish this finite operation from reset/counter increases."""
    def __init__(self, raw_limits):
        # Bootstrap raw is already post-metadata. Its cost belongs to S, and
        # must not be subtracted from this same observation a second time.
        self.remaining = {key: value["remaining"] for key, value in raw_limits.items()}
        self.pending = {key: 0 for key in raw_limits}

    def charge_graphql(self, cost):
        if type(cost) is not int or cost < 0:
            raise ValueError("invalid settled own GraphQL charge")
        self.pending["graphql"] += cost

    def observe(self, raw_limits):
        for key, value in raw_limits.items():
            self.remaining[key] = max(0, min(value["remaining"], self.remaining[key] - self.pending[key]))
            self.pending[key] = 0

    def projected_limits(self, raw_limits):
        value = copy.deepcopy(raw_limits)
        for key, resource in value.items():
            resource["remaining"] = max(0, min(resource["remaining"], self.remaining[key] - self.pending[key]))
            resource["used"] = resource["limit"] - resource["remaining"]
        return value
