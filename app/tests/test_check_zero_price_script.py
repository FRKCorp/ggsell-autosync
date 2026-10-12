"""scripts/check_zero_price.py целиком: на настоящем коде системы, с ответом
FZ, где цена лота подменена на 0."""

import importlib.util
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.position import Position, SourceType
from app.pricing.calculator import PricingConfig
from app.pricing.listing_price import listing_price_rub

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_zero_price.py"
CONFIG = PricingConfig(Decimal("95.0"), Decimal("15.0"), Decimal("3.0"), Decimal("5.0"))
OFFER_ID = 103461016


def load_script():
    spec = importlib.util.spec_from_file_location("check_zero_price", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        position = Position(external_id="giftcard:google_play_in:10_inr", source_type=SourceType.GIFTCARD,
                            fz_category_id="google_play_in", fz_offer_id="10_inr", region="IN",
                            name="Google Play (IN) — 10 INR", last_known_price_usd=Decimal("0.1162"),
                            raw_payload={"name": "10 INR"})
        s.add(position)
        s.flush()
        listing = Listing(position=position, ggsell_offer_id=OFFER_ID, status=ListingStatus.ACTIVE,
                          price_rub=Decimal(0), price_source_usd_at_sync=Decimal("0.1162"),
                          ggsell_fee=Decimal("0.04"), ggsell_payment_fee=Decimal("0.027"))
        listing.price_rub = listing_price_rub(listing, CONFIG)
        s.add(listing)
        s.commit()
        yield s


def run_script(session, fz_answer):
    module = load_script()
    real_fz = MagicMock()
    real_fz.get_giftcard_offers.side_effect = lambda category: fz_answer

    @contextmanager
    def fz_client(**kwargs):
        yield real_fz

    alerts = []
    with patch.object(module, "SessionLocal", return_value=session), \
         patch.object(module, "FazerCardsClient", side_effect=fz_client), \
         patch.object(module, "notify_admin", side_effect=alerts.append), \
         patch.object(module, "load_pricing_config", return_value=CONFIG), \
         patch.object(sys, "argv", ["check_zero_price.py", "--offer", str(OFFER_ID)]), \
         pytest.raises(SystemExit) as exit_info:
        module.main()
    return exit_info.value.code, alerts


def test_zero_price_is_blocked_end_to_end(session, capsys):
    answer = {"name": "Google Play (IN)", "offers": [{"card_id": "10_inr", "name": "10 INR", "price_usd": "0.1162"}]}
    listing = session.query(Listing).one()
    price_before = listing.price_rub

    code, alerts = run_script(session, answer)

    out = capsys.readouterr().out
    assert code == 0, out
    assert "✅ цена 0 не прошла" in out and "✅ товар у FazerCards не покупается" in out
    assert len(alerts) == 2 and all(a.startswith("🧪 ТЕСТ") for a in alerts)
    assert "неправильные данные" in alerts[0] and "НЕ сделан" in alerts[1]
    position = session.query(Position).one()
    assert position.last_known_price_usd == Decimal("0.1162")
    assert listing.price_rub == price_before


def test_fz_not_answering_makes_check_inconclusive(session, capsys):
    code, alerts = run_script(session, {"error": "Subscription is not active"})

    assert "проверка невозможна" in str(code)
    assert alerts == []
