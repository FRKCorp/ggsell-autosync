"""Выгружает с GGSell поддеревья категорий для всех наших игр/сервисов —
основа для подбора category_id каждого лота.

Для каждой игры/сервиса из data/selected_*_categories.json ищем корневые
узлы через GET /api_sellers/v2/categories/search?q=... (узлы вида
«Игры > X» / «Сервисы и соцсети > X» — ровно один уровень под разделом) и
обходим их детей на MAX_DEPTH уровней вниз через list_categories(parent_id).

Запуск: python scripts/fetch_ggsell_categories.py
Результат: data/ggsell_category_tree.json —
    {"<поисковый запрос>": {"fz_categories": [...], "roots": [узел + "children": [...]]}}
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellError, GGSellV2Client  # noqa: E402

MAX_DEPTH = 3  # уровней под корнем игры: «Genshin > Кристаллы > Регион > ...»
PAUSE_SECONDS = 0.3
RETRIES = 4  # GGSell периодически отвечает 504 Gateway Time-out под нагрузкой
# Разделы для запасного пути. «Игры» (id 33627) сюда НЕ входит: в нём
# 25 000+ подкатегорий (250+ страниц) — обход занимает больше часа; игры и
# так находятся поиском.
FALLBACK_SECTIONS = {"Сервисы и соцсети", "Программное обеспечение", "Цифровые товары"}

# Где название категории FZ (без региона) плохо ищется на GGSell — свой запрос.
QUERY_OVERRIDES = {
    # У GGSell нет отдельного «App Store»: карты — внутри «Apple ID».
    "App Store & iTunes": "Apple ID",
    # «EA» подстрокой совпадает с сотней узлов; корень — «Electronic Arts (EA)».
    "EA Gift Cards": "Electronic Arts",
    "ExitLag Gift Card Tier 1": "ExitLag",
    "ExitLag Gift Card Tier 2": "ExitLag",
    "ExitLag Gift Card Tier 3": "ExitLag",
    "League of Legends RP": "League of Legends",
    "Wild Rift WC": "Wild Rift",
    "Steam Wallet": "Steam",
    "Roblox Robux": "Roblox",
    "Apex Legends™": "Apex Legends",
    "Apex Legends Mobile": "Apex Legends",
    "PUBG: BATTLEGROUNDS": "PUBG",
    "Xbox Game Pass": "Xbox",
    "Call of Duty Mobile - Activision": "Call of Duty Mobile",
    "Undawn Global": "Undawn",
    "Arena Breakout: Infinite": "Arena Breakout",
    "PUBG: New State": "New State",
}


def items(response):
    return response.get("data", response) if isinstance(response, dict) else response


def with_retry(call, *args, **kwargs):
    for attempt in range(1, RETRIES + 1):
        try:
            return call(*args, **kwargs)
        except GGSellError as e:
            if e.status_code < 500 or attempt == RETRIES:
                raise
            time.sleep(2 * attempt)


def search(v2: GGSellV2Client, query: str) -> list[dict]:
    return items(with_retry(v2._request, "GET", "/api_sellers/v2/categories/search", params={"q": query}))


def list_children(v2: GGSellV2Client, parent_id: int) -> list[dict]:
    result, page = [], 1
    while True:
        response = with_retry(v2.list_categories, parent_id=parent_id, page=page)
        result.extend(items(response))
        pagination = response.get("pagination", {}) if isinstance(response, dict) else {}
        if not pagination.get("has_next_page"):
            return result
        page += 1
        time.sleep(PAUSE_SECONDS)


def crawl(v2: GGSellV2Client, node: dict, depth: int, counter: list[int]) -> dict:
    node = dict(node)
    if node.get("has_children") and depth < MAX_DEPTH:
        time.sleep(PAUSE_SECONDS)
        counter[0] += 1
        node["children"] = [crawl(v2, ch, depth + 1, counter) for ch in list_children(v2, node["id"])]
    return node


def is_root(node: dict) -> bool:
    # «Игры > Genshin Impact», «Сервисы и соцсети > Netflix» — ровно один «>».
    return node["tree"].count(">") == 1


def section_children(v2: GGSellV2Client, cache: dict) -> list[dict]:
    """Прямые дети верхних разделов — запасной путь, когда поиск по
    популярному названию (Steam, PlayStation) забит 100 листьями вида
    «Игра > Аккаунты > Steam» и сам корень в выдачу не попадает."""
    if "nodes" not in cache:
        cache["nodes"] = []
        for section in items(with_retry(v2.list_categories)):
            if section["title"] not in FALLBACK_SECTIONS:
                continue
            cache["nodes"].extend(list_children(v2, section["id"]))
            time.sleep(PAUSE_SECONDS)
    return cache["nodes"]


def find_roots(v2: GGSellV2Client, query: str, cache: dict) -> list[dict]:
    roots = [node for node in search(v2, query) if is_root(node)]
    if roots:
        return roots
    # По целым словам, а не подстрокой: «EA» не должно совпадать с «TunnelBear».
    pattern = re.compile(rf"(?<!\w){re.escape(query)}(?!\w)", re.IGNORECASE)
    return [node for node in section_children(v2, cache) if pattern.search(node["title"])]


def main() -> None:
    load_dotenv()
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(base, "data")

    def load(name):
        with open(os.path.join(data_dir, name), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else d.get("items", d)

    queries: dict[str, list[str]] = {}
    for selected, catalog in [
        ("selected_topup_categories.json", "fz_topup_categories.json"),
        ("selected_giftcard_categories.json", "fz_giftcard_categories.json"),
    ]:
        names = {c["category_id"]: c["name"] for c in load(catalog)}
        for item in load(selected):
            base_name = re.sub(r"\s*\([^)]*\)\s*$", "", names[item["category_id"]]).strip()
            query = QUERY_OVERRIDES.get(base_name, base_name)
            queries.setdefault(query, []).append(item["category_id"])

    out_path = os.path.join(data_dir, "ggsell_category_tree.json")
    result = {}
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            result = {q: r for q, r in json.load(f).items() if r.get("roots") and not r.get("error")}
    todo = [q for q in queries if q not in result]
    print(
        f"Поисковых запросов: {len(queries)}, уже собрано: {len(result)}, осталось: {len(todo)}",
        flush=True,
    )

    def save() -> None:
        # После каждого запроса — чтобы сбой или остановка не теряли собранное.
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)

    requests = [0]
    sections_cache: dict = {}
    with GGSellV2Client(
        api_key=os.getenv("GGSELL_API_KEY"),
        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
    ) as v2:
        for query in todo:
            fz_ids = queries[query]
            try:
                requests[0] += 1
                roots = [crawl(v2, node, 0, requests) for node in find_roots(v2, query, sections_cache)]
            except GGSellError as e:
                print(f"  ❌ {query}: GGSell {e.status_code} {str(e.payload)[:120]}", flush=True)
                result[query] = {"fz_categories": fz_ids, "roots": [], "error": str(e.payload)[:300]}
                save()
                continue
            result[query] = {"fz_categories": fz_ids, "roots": roots}
            save()
            print(
                f"  {'✅' if roots else '⚠️'} {query}: корней {len(roots)} — {[r['tree'] for r in roots]}",
                flush=True,
            )

    save()
    print(f"\n✅ Сохранено в {out_path}, запросов к API: {requests[0]}")
    missing = [q for q, r in result.items() if not r["roots"]]
    if missing:
        print(f"Без корня (искать вручную): {missing}")


if __name__ == "__main__":
    main()
