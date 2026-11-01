"""What buying a flat really costs: the price plus Austrian purchase costs.

The rates are typical values and can be changed in the profile's `costs:`
section. They are an estimate for comparing flats, not a quote: the notary's
or lawyer's fee in particular varies.

Money is calculated with Decimal, which represents amounts like 1.1 % exactly.
A float would not (0.1 + 0.2 == 0.30000000000000004), which is harmless for one
estimate but the wrong habit for money.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict

from immob.extract import parse_number
from immob.models import Listing

# A rate such as "3 %", but not the VAT in "zzgl. 20 % USt.".
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%(?!\s*(?:USt|MwSt))", re.IGNORECASE)
_AMOUNT = re.compile(r"(?:€|EUR)\s*(\d[\d.]*(?:,\d+)?)|(\d[\d.]*(?:,\d+)?)\s*(?:,-)?\s*(?:€|EUR)")
# "zzgl. 20% USt.", "zuzüglich USt.", "+ MwSt": the VAT comes on top.
_PLUS_VAT = re.compile(r"zzgl|zuzüglich|\+\s*(?:\d+\s*%\s*)?(?:USt|MwSt)", re.IGNORECASE)
_INCL_VAT = re.compile(r"inkl|inklusive", re.IGNORECASE)


class CostSettings(BaseModel):
    """Rates in percent of the price (plus an extra-cost garage)."""

    model_config = ConfigDict(extra="forbid")

    transfer_tax_percent: float = 3.5  # Grunderwerbsteuer
    land_registry_percent: float = 1.1  # Grundbuch-Eintragungsgebühr
    contract_percent: float = 1.5  # notary or lawyer: contract and trustee, plus VAT
    vat_percent: float = 20
    # Used when a listing doesn't say what the agent charges; the maximum
    # usual rate, so an unknown commission doesn't make a flat look cheaper.
    assumed_commission_percent: float = 3


@dataclass
class CostItem:
    label: str
    amount: Decimal
    note: str = ""


@dataclass
class PurchaseCosts:
    price: Decimal  # price plus a garage that costs extra
    items: list[CostItem] = field(default_factory=list)

    @property
    def extra(self) -> Decimal:
        return sum((item.amount for item in self.items), Decimal(0))

    @property
    def total(self) -> Decimal:
        return self.price + self.extra

    @property
    def commission_assumed(self) -> bool:
        return any(item.note == "assumed" for item in self.items)


def purchase_costs(listing: Listing, settings: CostSettings) -> PurchaseCosts | None:
    """The estimated total cost of buying the listing, or None without a price."""
    if listing.total_price_eur is None:
        return None
    price = Decimal(listing.total_price_eur)
    vat = 1 + _decimal(settings.vat_percent) / 100

    def share(percent: float | Decimal, factor: Decimal = Decimal(1)) -> Decimal:
        # Rounded once, at the end: rounding before adding VAT would be off by a euro.
        return _euros(price * _decimal(percent) / 100 * factor)

    costs = PurchaseCosts(price=price)
    costs.items += [
        CostItem(
            f"Transfer tax {settings.transfer_tax_percent:g} %",
            share(settings.transfer_tax_percent),
        ),
        CostItem(
            f"Land registry {settings.land_registry_percent:g} %",
            share(settings.land_registry_percent),
        ),
        CostItem(
            f"Contract {settings.contract_percent:g} % + VAT",
            share(settings.contract_percent, vat),
        ),
    ]

    commission = _commission(listing, price, vat, settings)
    if commission is not None:
        costs.items.append(commission)
    return costs


def _commission(
    listing: Listing, price: Decimal, vat: Decimal, settings: CostSettings
) -> CostItem | None:
    if listing.commission_free:
        return None
    text = listing.commission_text or ""
    plus_vat = not _INCL_VAT.search(text)  # Austrian commissions are quoted net by default

    match = _PERCENT.search(text)
    if match and 0 < (percent := _decimal(match.group(1).replace(",", "."))) <= 10:
        amount = price * percent / 100 * (vat if plus_vat else 1)
        label = f"Commission {percent.normalize():f} %" + (" + VAT" if plus_vat else "")
        return CostItem(label, _euros(amount), "from listing")
    if match := _AMOUNT.search(text):
        number = parse_number(match.group(1) or match.group(2))
        if number:
            amount = Decimal(str(number)) * (vat if plus_vat and _PLUS_VAT.search(text) else 1)
            return CostItem("Commission", _euros(amount), "from listing")

    percent = _decimal(settings.assumed_commission_percent)
    return CostItem(
        f"Commission {percent.normalize():f} % + VAT",
        _euros(price * percent / 100 * vat),
        "assumed",
    )


def _decimal(value: float | str | Decimal) -> Decimal:
    # Via str: Decimal(1.1) would be 1.100000000000000088817841970012523...
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _euros(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)
