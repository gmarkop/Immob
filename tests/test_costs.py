from decimal import Decimal

import pytest

from immob.costs import CostSettings, purchase_costs
from immob.filters import FilterProfile
from immob.models import Listing


def flat(**fields) -> Listing:
    return Listing(
        source="t", source_id="1", url="u", title="t", **({"price_eur": 149900} | fields)
    )


def test_breakdown_with_commission_from_listing():
    costs = purchase_costs(
        flat(commission_text="3% des Kaufpreises zzgl. 20% USt."), CostSettings()
    )

    assert [(item.label, item.amount, item.note) for item in costs.items] == [
        ("Transfer tax 3.5 %", Decimal(5247), ""),  # 5246.50, rounded half up
        ("Land registry 1.1 %", Decimal(1649), ""),  # 1648.90
        ("Contract 1.5 % + VAT", Decimal(2698), ""),  # 2248.50 × 1.2 = 2698.20, rounded once
        ("Commission 3 % + VAT", Decimal(5396), "from listing"),  # 4497 × 1.2
    ]
    assert costs.total == Decimal(149900 + 5247 + 1649 + 2698 + 5396)
    assert not costs.commission_assumed


@pytest.mark.parametrize(
    ("text", "free", "label", "amount", "note"),
    [
        ("3.00 % zuzüglich USt.", None, "Commission 3 % + VAT", 5396, "from listing"),  # IS24
        ("2,5 % inkl. USt.", None, "Commission 2.5 %", 3748, "from listing"),
        ("€ 4.500,- inkl. USt.", None, "Commission", 4500, "from listing"),
        ("zzgl. 20 % USt.", None, "Commission 3 % + VAT", 5396, "assumed"),  # VAT, not a rate
        (None, None, "Commission 3 % + VAT", 5396, "assumed"),
        (None, True, None, None, None),  # provisionsfrei
    ],
)
def test_commission(text, free, label, amount, note):
    costs = purchase_costs(flat(commission_text=text, commission_free=free), CostSettings())
    commission = [item for item in costs.items if item.label.startswith("Commission")]
    if label is None:
        assert commission == []
    else:
        [item] = commission
        assert (item.label, item.amount, item.note) == (label, Decimal(amount), note)


def test_garage_that_costs_extra_is_part_of_the_base():
    costs = purchase_costs(
        flat(has_garage=True, garage_price_eur=18000, commission_free=True), CostSettings()
    )
    assert costs.price == Decimal(167900)
    assert costs.items[0].amount == Decimal(5877)  # 3.5 % of 167,900 (5876.50)


def test_rates_come_from_settings():
    settings = CostSettings(transfer_tax_percent=3.5, land_registry_percent=0, contract_percent=2)
    costs = purchase_costs(flat(commission_free=True), settings)
    assert costs.items[1].amount == 0
    assert costs.items[2].amount == Decimal(3598)  # 2 % of 149,900 = 2998, + 20 % VAT


def test_no_price_no_costs():
    assert purchase_costs(flat(price_eur=None), CostSettings()) is None


def test_total_cost_filter():
    profile = FilterProfile(total_cost_max=160000)
    assert profile.matches(flat(commission_free=True))  # 159,494
    assert profile.rejections(flat(commission_free=False)) == ["total cost 164890 > 160000"]
    assert profile.matches(flat(price_eur=None))  # unknown passes with include_unknown
