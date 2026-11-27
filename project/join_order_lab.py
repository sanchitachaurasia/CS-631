"""Demonstrate how row-count feedback can change a join-order decision."""

import argparse
import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Dimension:
    name: str
    key_count: int
    matching_keys: int

    @property
    def selectivity(self) -> float:
        return self.matching_keys / self.key_count

    def draw_key(self, rng: random.Random, matching_share: float) -> int:
        if rng.random() < matching_share:
            return rng.randrange(self.matching_keys)
        return rng.randrange(self.matching_keys, self.key_count)

    def matches(self, key: int) -> bool:
        return key < self.matching_keys


CUSTOMERS = Dimension("Customers first", key_count=100, matching_keys=10)
PRODUCTS = Dimension("Products first", key_count=100, matching_keys=30)
DIMENSIONS = (CUSTOMERS, PRODUCTS)


@dataclass(frozen=True)
class JoinResult:
    intermediate_rows: int
    order_ids: tuple[int, ...]


def make_orders(count: int, seed: int, north_share: float,
                featured_share: float) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    return [
        (CUSTOMERS.draw_key(rng, north_share), PRODUCTS.draw_key(rng, featured_share))
        for _ in range(count)
    ]


def execute(orders: list[tuple[int, int]], first: int) -> JoinResult:
    """Each join looks up one dimension key and applies that dimension's filter."""
    second = 1 - first
    first_dim, second_dim = DIMENSIONS[first], DIMENSIONS[second]
    intermediate_rows = 0
    matched_orders = []
    for order_id, order in enumerate(orders, start=1):
        if first_dim.matches(order[first]):
            intermediate_rows += 1
            if second_dim.matches(order[second]):
                matched_orders.append(order_id)
    return JoinResult(intermediate_rows, tuple(matched_orders))


def execute_both(orders: list[tuple[int, int]]) -> tuple[JoinResult, JoinResult]:
    customers_first = execute(orders, 0)
    products_first = execute(orders, 1)
    if customers_first.order_ids != products_first.order_ids:
        raise RuntimeError("join orders returned different order IDs")
    return customers_first, products_first


def preferred_order(row_counts: tuple[float, float]) -> int:
    # Both plans visit every order in stage one. A smaller intermediate
    # result reduces the number of dimension lookups in stage two.
    return min(range(len(DIMENSIONS)), key=row_counts.__getitem__)


def print_choices(estimates: tuple[float, float], observations: tuple[JoinResult, JoinResult],
                  label: str) -> None:
    selected = preferred_order(estimates)
    print(f"\n{label}")
    print(f"{'Join order':<18} {'Estimated':>12} {'Actual':>12} {'Selected':>10}")
    for index, dimension in enumerate(DIMENSIONS):
        actual = observations[index].intermediate_rows
        marker = "yes" if index == selected else ""
        print(f"{dimension.name:<18} {estimates[index]:>12,.1f} {actual:>12,} {marker:>10}")


def show_workload(order_count: int, seed: int, north_share: float, featured_share: float,
                  next_north: float | None, next_featured: float | None) -> None:
    orders = make_orders(order_count, seed, north_share, featured_share)
    observed = execute_both(orders)
    initial = (order_count * CUSTOMERS.selectivity, order_count * PRODUCTS.selectivity)
    feedback = (observed[0].intermediate_rows, observed[1].intermediate_rows)

    print("Orders joined to customers and products")
    print("Filters select northern customers and featured products.")
    print(f"Generated {len(orders):,} orders using seed {seed}.")
    print(f"Order generation targets {north_share:.0%} northern and {featured_share:.0%} featured.")
    print("The planner minimizes the expected number of rows entering the second join.")
    print_choices(initial, observed, "Before feedback, using dimension selectivity")
    print_choices(feedback, observed, "After observing both candidate join orders")

    avoided = feedback[preferred_order(initial)] - feedback[preferred_order(feedback)]
    print(f"\nBoth orders return {len(observed[0].order_ids):,} identical order IDs.")
    print(f"Feedback avoids {avoided:,} second-stage lookups on this dataset.")

    if next_north is not None or next_featured is not None:
        north = north_share if next_north is None else next_north
        featured = featured_share if next_featured is None else next_featured
        changed = execute_both(make_orders(order_count, seed + 1, north, featured))
        fresh = (changed[0].intermediate_rows, changed[1].intermediate_rows)
        print(f"\nNew data targets {north:.0%} northern and {featured:.0%} featured.")
        print_choices(feedback, changed, "New distribution, reusing the old observations")
        print_choices(fresh, changed, "New distribution, refreshing the observations")
        extra = fresh[preferred_order(feedback)] - fresh[preferred_order(fresh)]
        print(f"\nReusing the old counts adds {extra:,} second-stage lookups on the new dataset.")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("enter a positive order count")
    return parsed


def share(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("enter a fraction between 0 and 1")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=positive_int, default=10_000, help="number of synthetic orders")
    parser.add_argument("--seed", type=int, default=631, help="random seed for data generation")
    parser.add_argument("--north-share", type=share, default=0.70,
                        help="fraction of orders drawn from northern customers")
    parser.add_argument("--featured-share", type=share, default=0.15,
                        help="fraction of orders drawn from featured products")
    parser.add_argument("--next-north-share", type=share,
                        help="northern customer share for a second, changed dataset")
    parser.add_argument("--next-featured-share", type=share,
                        help="featured product share for a second, changed dataset")
    args = parser.parse_args()
    show_workload(args.orders, args.seed, args.north_share, args.featured_share,
                  args.next_north_share, args.next_featured_share)


if __name__ == "__main__":
    main()
