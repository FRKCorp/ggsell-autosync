from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.ggsell import GGSellError
from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.position import Position, SourceType
from app.offers.text_update import replace_texts, update_offer_texts

PAIRS = [("до 01:00", "до 22:00"), ("to 01:00", "to 22:00")]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_listing(session, offer_id, status=ListingStatus.DRAFT):
    position = Position(external_id=f"p{offer_id}", source_type=SourceType.TOPUP, name="x",
                        last_known_price_usd=Decimal("1"), raw_payload={})
    session.add(Listing(position=position, ggsell_offer_id=offer_id, status=status, price_rub=Decimal("100"),
                        price_source_usd_at_sync=Decimal("1")))
    session.commit()


def offer(offer_id, **texts):
    return {"id": offer_id, "status": "draft", **texts}


def run(session, v2, offers, **kwargs):
    return update_offer_texts(session, v2, offers, PAIRS, pause_seconds=0, sleep=lambda s: None, **kwargs)


def test_replace_texts_only_changed_fields_and_keeps_manual_edits():
    o = offer(1, description_ru="Ручная правка. Поддержка с 11 до 01:00 по мск.",
              description_en="Support from 11:00 to 01:00 Moscow time.",
              instructions_ru="Без часов поддержки.", instructions_en=None)

    assert replace_texts(o, PAIRS) == {
        "description_ru": "Ручная правка. Поддержка с 11 до 22:00 по мск.",
        "description_en": "Support from 11:00 to 22:00 Moscow time.",
    }


def test_patches_only_our_lots_with_changed_fields(session):
    add_listing(session, 1)
    add_listing(session, 2)  # тексты уже новые
    add_listing(session, 3, status=ListingStatus.ARCHIVED)
    add_listing(session, 4)  # нет на GGSell
    offers = {
        1: offer(1, instructions_ru="Поддержка с 11:00 до 01:00 по МСК."),
        2: offer(2, instructions_ru="Поддержка с 11:00 до 22:00 по МСК."),
        3: offer(3, instructions_ru="Поддержка с 11:00 до 01:00 по МСК."),
        99: offer(99, description_ru="Чужой товар продавца, до 01:00"),
    }
    v2 = MagicMock()

    report = run(session, v2, offers)

    v2.patch_offer.assert_called_once_with(1, {"instructions_ru": "Поддержка с 11:00 до 22:00 по МСК."})
    assert (report.updated, report.unchanged, report.missing_on_ggsell) == (1, 1, 1)


def test_dry_run_does_not_patch(session):
    add_listing(session, 1)
    v2 = MagicMock()

    report = run(session, v2, {1: offer(1, description_en="from 11:00 to 01:00")}, dry_run=True)

    v2.patch_offer.assert_not_called()
    assert report.updated == 1


def test_error_on_one_lot_does_not_stop_others(session):
    add_listing(session, 1)
    add_listing(session, 2)
    v2 = MagicMock()
    v2.patch_offer.side_effect = [GGSellError(422, "invalid"), {"data": {}}]
    offers = {i: offer(i, description_ru="до 01:00") for i in (1, 2)}

    report = run(session, v2, offers)

    assert report.updated == 1 and list(report.errors) == [1]
