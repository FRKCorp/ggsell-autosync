import base64
from decimal import Decimal

import pytest

from app.models.position import Position, SourceType
from app.offers.builder import build_offer, format_item, game_name, region_text, load_templates
from app.pricing.calculator import PricingConfig

CONFIG = PricingConfig(
    exchange_rate_usd_to_rub=Decimal("87.7367"),
    markup_percent=Decimal("15"),
    min_margin_percent=Decimal("3"),
    max_price_deviation_percent=Decimal("5"),
)
MLBB_FIELDS = [
    {"key": "player_id", "label": "Player ID", "type": "text"},
    {"key": "server_id", "label": "Server ID", "type": "text"},
]
WUWA_FIELDS = [
    {"key": "player_id", "label": "Player ID", "type": "text"},
    {"key": "server", "label": "Server", "type": "select", "options": [{"label": "Asia", "value": "os_asia"}]},
]
CURRENCY_CATEGORY = {"category_id": 100319013, "fee": 0.04, "payment_fee": 0.027}
CARD_CATEGORY = {"category_id": 115011, "fee": 0.04, "payment_fee": 0.027}


def position(name, offer_name, source_type=SourceType.TOPUP, region="Global", variant=None, price="4.6857"):
    return Position(
        external_id="x",
        source_type=source_type,
        name=name,
        region=region,
        variant_label=variant,
        last_known_price_usd=Decimal(price),
        raw_payload={"name": offer_name},
    )


@pytest.mark.parametrize("offer_name, source_type, region, ru, en", [
    ("250 + 25 Diamonds", SourceType.TOPUP, "RU", "Алмазы 275 (250+25)", "Diamonds 275 (250+25)"),
    ("60 UC", SourceType.TOPUP, "Global", "UC 60", "UC 60"),
    ("20 000 Coins", SourceType.TOPUP, "Global", "Монеты 20000", "Coins 20000"),
    ("AUR125", SourceType.TOPUP, "Global", "AUR 125", "AUR 125"),
    ("Weekly Pass", SourceType.TOPUP, "RU", "Недельный пропуск", "Weekly Pass"),
    ("1 Month (UK) Standard", SourceType.TOPUP, "UK", "Standard 1 мес.", "Standard 1 mo."),
    ("(iOS) 5 Rainbow Cards", SourceType.TOPUP, "Global", "Радужные карты 5 (iOS)", "Rainbow Cards 5 (iOS)"),
    ("300 Crystals + 30 Diamonds", SourceType.TOPUP, "Global", "Кристаллы 300 + Алмазы 30", "Crystals 300 + Diamonds 30"),
    ("Level Up Package 6", SourceType.TOPUP, "CIS", "Пакет прокачки 6", "Level Up Package 6"),
    ("Golden Spin", SourceType.TOPUP, "Global", "Golden Spin", "Golden Spin"),  # нет в словаре — как у FZ
    ("25 USD", SourceType.GIFTCARD, "US", "Подарочная карта 25 USD", "Gift card 25 USD"),
    ("ExitLag: 1 Month Subscription", SourceType.GIFTCARD, "Global", "Подписка ExitLag 1 мес.", "ExitLag: Subscription 1 mo."),
])
def test_format_item(offer_name, source_type, region, ru, en):
    assert format_item(offer_name, source_type=source_type, region=region, lang="ru") == ru
    assert format_item(offer_name, source_type=source_type, region=region, lang="en") == en


@pytest.mark.parametrize("name, region, variant, expected", [
    ("Mobile Legends (RU) — 35 Diamonds", "RU", None, "Mobile Legends"),
    ("PUBG Mobile (Auto, Global) — 60 UC", "Global", "Auto", "PUBG Mobile"),
    ("Apex Legends™ (EA, Global) — 1000 coins", "Global", None, "Apex Legends™ (EA)"),  # платформу не теряем
    ("CapCut — 1 Month (UK) Standard", "UK", None, "CapCut"),
])
def test_game_name(name, region, variant, expected):
    assert game_name(position(name, "x", region=region, variant=variant)) == expected


def test_region_texts():
    t = load_templates()
    assert region_text("RU", SourceType.TOPUP, "ru", t) == "Для России, Украины, Казахстана и Беларуси"
    assert region_text("IN", SourceType.TOPUP, "ru", t) == "Для аккаунтов региона: Индия"
    assert region_text("US", SourceType.GIFTCARD, "ru", t) == "Только для аккаунтов региона США"
    assert region_text("US", SourceType.GIFTCARD, "en", t) == "Only for accounts of the region: USA"


def test_topup_offer_matches_client_sample():
    """Образец клиента: «Mobile Legends RU* Алмазы 275 (250+25) | по ID | Автодоставка»."""
    p = position("Mobile Legends (RU) — 250 + 25 Diamonds", "250 + 25 Diamonds", region="RU")

    draft = build_offer(p, CURRENCY_CATEGORY, CONFIG, webhook_url="https://example.test/webhooks/ggsell", fz_fields=MLBB_FIELDS)
    pl = draft.payload

    assert pl["title_ru"] == "Mobile Legends RU* Алмазы 275 (250+25) | по ID | Автодоставка"
    assert pl["title_en"] == "Mobile Legends RU* Diamonds 275 (250+25) | by ID | Auto delivery"
    assert "пополнение по ID игрока и ID сервера" in pl["description_ru"]
    assert "Для России, Украины, Казахстана и Беларуси" in pl["description_ru"]
    assert "каждый в соответствующее поле" in pl["description_ru"]
    assert "ВАЖНО ПРО АВТОВЫДАЧУ" in pl["description_ru"] and "до 20%" in pl["description_ru"]
    assert "выберите сервер" not in pl["description_ru"]  # у MLBB нет выбора сервера
    assert pl["delivery"] == "auto"
    assert pl["category_id"] == 100319013
    assert pl["currency"] == "RUB"
    assert pl["notification_settings"]["url"] == "https://example.test/webhooks/ggsell"
    assert pl["notification_settings"]["http_method"] == "POST"
    assert draft.fz_fields == MLBB_FIELDS


def test_price_includes_rate_markup_fees_rounded_up():
    p = position("Mobile Legends (RU) — 275 Diamonds", "275 Diamonds", region="RU", price="4.6857")
    draft = build_offer(p, CURRENCY_CATEGORY, CONFIG, webhook_url=None, fz_fields=MLBB_FIELDS)
    # 4.6857 * 87.7367 * 1.15 / (1 − 0.067) = 506.73… → 507
    assert draft.price_rub == Decimal("507")
    assert draft.payload["price"] == 507.0
    assert draft.fees.total == Decimal("0.067")
    assert "notification_settings" not in draft.payload
    # Индивидуальная наценка лота
    assert build_offer(p, CURRENCY_CATEGORY, CONFIG, webhook_url=None, markup_percent=Decimal("0")).price_rub == Decimal("441")


def test_select_field_gets_server_note_not_input_rule():
    p = position("Wuthering Waves (Global) — 60 Lunite", "60 Lunite")
    pl = build_offer(p, CURRENCY_CATEGORY, CONFIG, webhook_url=None, fz_fields=WUWA_FIELDS).payload
    assert "пополнение по ID игрока и серверу" in pl["description_ru"]
    assert "вводите ID игрока в соответствующее поле" in pl["description_ru"]
    assert "«— выберите сервер —»" in pl["description_ru"]


def test_giftcard_offer():
    p = position("Google Play (US) — 25 USD", "25 USD", source_type=SourceType.GIFTCARD, region="US", price="24.58")
    draft = build_offer(p, CARD_CATEGORY, CONFIG, webhook_url=None, fz_fields=MLBB_FIELDS)
    pl = draft.payload
    assert pl["title_ru"] == "Google Play US* Подарочная карта 25 USD | Код | Автодоставка"
    assert "цифровой код, приходит в чат заказа" in pl["description_ru"]
    assert "Только для аккаунтов региона США" in pl["description_ru"]
    assert "Активируйте его в Google Play" in pl["instructions_ru"]
    assert draft.fz_fields == []  # у карт опций нет


def test_cover_is_placeholder_png():
    p = position("8 Ball Pool (Global) — Golden Spin", "Golden Spin")
    cover = build_offer(p, CURRENCY_CATEGORY, CONFIG, webhook_url=None).payload["cover_image_ru"]
    assert cover.startswith("data:image/png;base64,")
    assert base64.b64decode(cover.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"


def test_unsupported_source_type():
    p = position("CS2", "Prime", source_type=SourceType.STEAM_GIFT)
    with pytest.raises(ValueError):
        build_offer(p, CURRENCY_CATEGORY, CONFIG, webhook_url=None)


def test_long_title_shortened_to_ggsell_limit():
    """Arena Breakout: «Quarterly Premium Battle Pass Bundle + Activation Pass
    Bundle» — английское название вышло длиннее 100 символов, GGSell отказал."""
    long_name = "Quarterly Premium Battle Pass Bundle + Activation Pass Bundle + Extra Supply Crate Bonus"
    draft = build_offer(position("Arena Breakout — " + long_name, long_name), CURRENCY_CATEGORY, CONFIG,
                        webhook_url=None, fz_fields=MLBB_FIELDS)

    for lang in ("ru", "en"):
        title = draft.payload[f"title_{lang}"]
        assert len(title) <= 100
        assert title.startswith("Arena Breakout Global* Quarterly")
        assert "…" in title and title.endswith("Автодоставка" if lang == "ru" else "Auto delivery")
