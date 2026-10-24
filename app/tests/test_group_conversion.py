"""Перевод отдельных лотов App Store в групповые (scripts/group_offers.py)."""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Listing, ListingStatus, ListingVariant, Position, SourceType
from app.models.base import Base
from app.offers.group_conversion import DELETE, convert_groups
from app.offers.groups import group_positions
from app.sync.showcase import check_availability
from app.tests.test_group_uploader import CATEGORY, CONFIG, RULES, FakeGGSell

CAT = "app_store_itunes_tr"


class FakeBatchGGSell(FakeGGSell):
    """Плюс batch_pause / batch_delete: асинхронно, через job_id."""

    BATCH_LIMIT = 100

    def __init__(self):
        super().__init__()
        self.jobs: dict[str, dict] = {}
        self.patches: list[tuple[int, dict]] = []
        self.fail_batch = False

    def add_offer(self, title, price, status="active"):
        offer_id = next(self.ids)
        self.offers[offer_id] = {"id": offer_id, "title_ru": title, "price": price, "status": status,
                                 "cover_image_ru": "cover-by-client"}
        self.options[offer_id] = []
        return offer_id

    def patch_offer(self, offer_id, payload):
        self.patches.append((offer_id, payload))
        return super().patch_offer(offer_id, payload)

    def _job(self, offer_ids, status):
        job_id = f"job-{len(self.jobs)}"
        done = 0 if self.fail_batch else len(offer_ids)
        if not self.fail_batch:
            for offer_id in offer_ids:
                if status == "archived":
                    self.offers.pop(offer_id)
                else:
                    self.offers[offer_id]["status"] = status
        self.jobs[job_id] = {"status": "completed", "results": {"success_count": done}}
        return {"success": True, "job_id": job_id}

    def batch_pause_offers(self, offer_ids):
        return self._job(offer_ids, "paused")

    def batch_delete_offers(self, offer_ids):
        return self._job(offer_ids, "archived")

    def get_async_job_result(self, job_id):
        return {"data": self.jobs[job_id]}

    def showcase(self):
        """Как fetch_offers: архивных (удалённых) в выдаче нет."""
        return {i: dict(o) for i, o in self.offers.items()}


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_single_lots(session, v2, *nominals, status="active"):
    """Отдельные лоты номиналов — как их залил старый автозалив."""
    lots = []
    for n, usd, rub in nominals:
        position = Position(external_id=f"giftcard:{CAT}:{n}_try", source_type=SourceType.GIFTCARD,
                            fz_category_id=CAT, fz_offer_id=f"{n}_try", region="TR",
                            name=f"App Store & iTunes (TR) — {n} TRY", raw_payload={"name": f"{n} TRY"},
                            last_known_price_usd=Decimal(usd))
        offer_id = v2.add_offer(f"App Store & iTunes TR* {n} TRY", rub, status)
        lots.append(Listing(position=position, ggsell_offer_id=offer_id, status=status, price_rub=Decimal(rub),
                            price_source_usd_at_sync=Decimal(usd), ggsell_category_id=110400,
                            ggsell_fee=Decimal("0.06"), ggsell_payment_fee=Decimal("0.027"), options_attached=True))
    session.add_all(lots)
    session.commit()
    return lots


def run(session, v2, **kwargs):
    positions = session.query(Position).all()
    groups = group_positions(positions, RULES)
    return convert_groups(session, v2, groups, v2.showcase(), {p.external_id: CATEGORY for p in positions}, CONFIG,
                          webhook_url="https://x/webhooks/ggsell", pause_seconds=0, sleep=lambda s: None, **kwargs)


NOMINALS = ((50, "1.02", 110), (10, "0.21", 23), (2000, "41.04", 4326), (5000, "110", 11000))


def test_min_nominal_lot_becomes_group_others_paused(session):
    v2 = FakeBatchGGSell()
    lots = add_single_lots(session, v2, *NOMINALS)
    base_lot = lots[1]  # 10 TRY

    report = run(session, v2)

    assert report.errors == {} and report.converted == {CAT: base_lot.ggsell_offer_id}
    assert report.over_limit == {CAT: 1}  # 5000 TRY дороже лимита — его лот тоже на паузу
    session.refresh(base_lot)
    assert base_lot.is_group and base_lot.status == ListingStatus.ACTIVE and base_lot.position.fz_offer_id == "10_try"
    offer = v2.offers[base_lot.ggsell_offer_id]
    assert "Подарочная карта (TRY)" in offer["title_ru"] and offer["cover_image_ru"] == "cover-by-client"
    assert offer["price"] == float(base_lot.price_rub)
    assert "cover_image_ru" not in v2.patches[0][1] and "category_id" not in v2.patches[0][1]
    assert [v["title_ru"] for v in v2.variants(base_lot.ggsell_offer_id)] == ["10 TRY", "50 TRY", "2000 TRY"]
    assert session.query(ListingVariant).count() == 3
    others = [l for l in lots if l is not base_lot]
    assert sorted(report.retired[CAT]) == sorted(l.ggsell_offer_id for l in others)
    assert all(v2.offers[l.ggsell_offer_id]["status"] == "paused" for l in others)
    assert all(l.status == ListingStatus.ARCHIVED for l in others)


def test_rerun_changes_nothing(session):
    v2 = FakeBatchGGSell()
    add_single_lots(session, v2, *NOMINALS)
    run(session, v2)
    patches, jobs, variant_calls = len(v2.patches), len(v2.jobs), v2.variant_calls

    report = run(session, v2)

    assert report.converted == {} and report.existing == 1 and report.retired == {} and report.errors == {}
    assert (len(v2.patches), len(v2.jobs), v2.variant_calls) == (patches, jobs, variant_calls)


def test_delete_after_pause_removes_paused_lots(session):
    v2 = FakeBatchGGSell()
    lots = add_single_lots(session, v2, *NOMINALS)
    run(session, v2)

    report = run(session, v2, mode=DELETE)

    assert len(report.retired[CAT]) == 3
    assert set(v2.offers) == {lots[1].ggsell_offer_id}


def test_dry_run_touches_nothing(session):
    v2 = FakeBatchGGSell()
    lots = add_single_lots(session, v2, *NOMINALS)

    report = run(session, v2, dry_run=True)

    assert report.converted == {CAT: lots[1].ggsell_offer_id} and len(report.retired[CAT]) == 3
    assert v2.patches == [] and v2.jobs == {} and session.query(ListingVariant).count() == 0
    assert not any(l.is_group for l in lots) and all(l.status == ListingStatus.ACTIVE for l in lots)


def test_variants_failure_keeps_single_lots(session):
    v2 = FakeBatchGGSell()
    lots = add_single_lots(session, v2, *NOMINALS)
    real = v2.create_or_update_variants

    def broken(*args, **kwargs):
        raise ConnectionError("GGSell недоступен")
    v2.create_or_update_variants = broken

    report = run(session, v2)

    assert CAT in report.errors and "отдельные лоты не" in report.errors[CAT]
    assert v2.jobs == {} and all(o["status"] == "active" for o in v2.offers.values())
    assert all(l.status == ListingStatus.ACTIVE for l in lots if not l.is_group)

    v2.create_or_update_variants = real
    report = run(session, v2)  # повтор доделывает

    assert report.errors == {} and report.existing == 1 and len(report.retired[CAT]) == 3


def test_failed_batch_leaves_db_untouched_and_rerun_retries(session):
    v2 = FakeBatchGGSell()
    lots = add_single_lots(session, v2, *NOMINALS)
    v2.fail_batch = True

    report = run(session, v2)

    assert "обработал 0 из 3" in report.errors[CAT]
    assert all(l.status == ListingStatus.ACTIVE for l in lots if not l.is_group)

    v2.fail_batch = False
    report = run(session, v2)

    assert report.errors == {} and len(report.retired[CAT]) == 3


def test_drafts_and_deleted_lots_not_paused(session):
    v2 = FakeBatchGGSell()
    lots = add_single_lots(session, v2, (10, "0.21", 23), (50, "1.02", 110))
    draft = add_single_lots(session, v2, (100, "2.02", 210), status="draft")[0]
    gone = add_single_lots(session, v2, (200, "4.04", 420))[0]
    v2.offers.pop(gone.ggsell_offer_id)  # удалён на GGSell вручную

    report = run(session, v2)

    assert report.retired[CAT] == [lots[1].ggsell_offer_id]
    assert v2.offers[draft.ggsell_offer_id]["status"] == "draft"
    assert draft.status == ListingStatus.ARCHIVED and gone.status == ListingStatus.ARCHIVED


def test_cheapest_lot_used_when_base_nominal_has_no_lot(session):
    v2 = FakeBatchGGSell()
    lots = add_single_lots(session, v2, (50, "1.02", 110), (2000, "41.04", 4326))
    session.add(Position(external_id=f"giftcard:{CAT}:10_try", source_type=SourceType.GIFTCARD, fz_category_id=CAT,
                         fz_offer_id="10_try", region="TR", name="App Store & iTunes (TR) — 10 TRY",
                         raw_payload={"name": "10 TRY"}, last_known_price_usd=Decimal("0.21")))
    session.commit()

    report = run(session, v2)

    assert report.converted == {CAT: lots[0].ggsell_offer_id}
    assert lots[0].position.fz_offer_id == "10_try"  # база группы — самый дешёвый номинал FZ
    assert [v["title_ru"] for v in v2.variants(lots[0].ggsell_offer_id)] == ["10 TRY", "50 TRY", "2000 TRY"]


def test_category_without_lots_left_for_upload(session):
    v2 = FakeBatchGGSell()
    session.add(Position(external_id=f"giftcard:{CAT}:10_try", source_type=SourceType.GIFTCARD, fz_category_id=CAT,
                         fz_offer_id="10_try", region="TR", name="App Store & iTunes (TR) — 10 TRY",
                         raw_payload={"name": "10 TRY"}, last_known_price_usd=Decimal("0.21")))
    session.commit()

    report = run(session, v2)

    assert report.nothing == [CAT] and v2.patches == []


def test_group_nominals_get_availability_alerts(session):
    v2 = FakeBatchGGSell()
    lots = add_single_lots(session, v2, *NOMINALS)
    run(session, v2)
    nominal_50 = lots[0].position  # теперь вариант группового лота, а не отдельный лот
    alerts = []

    check_availability(session, [(nominal_50, None, "FazerCards error 404: product not found")], alert=alerts.append)

    assert alerts and "50 TRY" in alerts[0]
