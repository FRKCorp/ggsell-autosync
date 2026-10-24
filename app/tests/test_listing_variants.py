from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Listing, ListingStatus, ListingVariant, Order, OrderStatus, Position, SourceType
from app.models.base import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def card(nominal, price):
    return Position(external_id=f"giftcard:app_store_itunes_tr:{nominal}_try", source_type=SourceType.GIFTCARD,
                    fz_category_id="app_store_itunes_tr", fz_offer_id=f"{nominal}_try", region="TR",
                    name=f"App Store & iTunes (TR) — {nominal} TRY", last_known_price_usd=Decimal(price))


def group(session):
    cards = [card(25, "0.75"), card(50, "1.49"), card(100, "2.97")]
    session.add_all(cards)
    session.flush()
    listing = Listing(position=cards[0], ggsell_offer_id=103720161, status=ListingStatus.DRAFT,
                      price_rub=Decimal("30"), price_source_usd_at_sync=Decimal("0.75"),
                      is_group=True, ggsell_option_id=8974650)
    for i, (position, price, variant_id) in enumerate(zip(cards, ("30", "55", "90.50"), (72390560, 72390562, 72390564))):
        listing.variants.append(ListingVariant(position=position, ggsell_variant_id=variant_id,
                                               price_rub=Decimal(price),
                                               price_source_usd_at_sync=position.last_known_price_usd,
                                               sort_order=i))
    session.add(listing)
    session.commit()
    return listing, cards


def test_group_listing_with_variants(session):
    listing, cards = group(session)

    session.expire_all()
    listing = session.get(Listing, listing.id)
    assert listing.is_group and listing.position.external_id.endswith(":25_try")
    assert [v.position.fz_offer_id for v in listing.variants] == ["25_try", "50_try", "100_try"]
    assert [v.price_rub - listing.price_rub for v in listing.variants] == [0, 25, Decimal("60.50")]
    assert all(v.status == "active" for v in listing.variants)


def test_ordinary_listing_defaults(session):
    position = card(25, "0.75")
    session.add(Listing(position=position, ggsell_offer_id=1, price_rub=Decimal("30"),
                        price_source_usd_at_sync=Decimal("0.75")))
    session.commit()

    listing = session.query(Listing).one()
    assert listing.is_group is False and listing.ggsell_option_id is None and listing.variants == []


def test_position_belongs_to_one_group_only(session):
    listing, cards = group(session)
    other = Listing(position=cards[1], ggsell_offer_id=2, price_rub=Decimal("1"),
                    price_source_usd_at_sync=Decimal("1"), is_group=True)
    other.variants.append(ListingVariant(position=cards[1], price_rub=Decimal("1"),
                                         price_source_usd_at_sync=Decimal("1")))
    session.add(other)

    with pytest.raises(IntegrityError):
        session.commit()


def test_order_keeps_bought_nominal(session):
    listing, cards = group(session)
    session.add(Order(ggsell_invoice_id="1", listing=listing, position=cards[1], status=OrderStatus.PENDING,
                      buyer_data={"Номинал": "50 TRY"}, price_at_sale_rub=Decimal("55")))
    session.commit()

    order = session.query(Order).one()
    assert order.position.fz_offer_id == "50_try" and order.listing.is_group
