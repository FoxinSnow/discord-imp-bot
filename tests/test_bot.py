"""Offline business tests. No Discord login, STRATZ request, or token needed."""

from __future__ import annotations

import asyncio
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import aiohttp
import discord
from discord.ext import commands

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from impbot.core import (
    ConfigError, DEFAULT_MATCH_COUNT, MAX_MATCH_COUNT, Match, PlayerHistory, Rule, STEAM_ID_BASE, UserError,
    format_score, load_rules, load_unavailable, parse_friend_code, render_text, select_rule, summarize,
    validate_match_count,
)
from impbot.discord_app import (
    COMMAND_NAME, COMMAND_PREFIX, REBOOT_ADMIN_USER_ID, ImpClient, make_embed,
)
from impbot.stratz import QUERY, StratzClient, parse_history, parse_valid_history

ACCOUNT = 123456789
REPLY_TEXT = "目标玩家近期平均IMP为：{average}\n一眼盯真 鉴定为"


def payload(records=None, *, name="测试玩家"):
    return {"data": {"player": {"steamAccount": {"name": name},
                                "matches": records if records is not None else []}}}


def record(match_id, time, imp=0, *, target=ACCOUNT):
    return {"id": match_id, "startDateTime": time, "players": [
        {"steamAccountId": 987654321, "imp": 999},
        {"steamAccountId": target, "imp": imp},
    ]}


def history(scores, *, requested_count=None):
    return PlayerHistory(ACCOUNT, "测试玩家", tuple(
        Match(1000 + i, 2000 - i, score) for i, score in enumerate(scores)
    ), requested_count if requested_count is not None else (len(scores) or DEFAULT_MATCH_COUNT))


def write_config(root, *, image=None, unavailable_image=None):
    config = {"rules": [
        {"min": None, "title": "低分", "text": REPLY_TEXT, "image": image},
        {"min": 0, "title": "高分", "text": REPLY_TEXT, "image": image},
    ], "unavailable": {"text": "神秘嘉宾来袭", "image": unavailable_image}}
    (root / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return config


class CoreTests(unittest.TestCase):
    def test_friend_code_and_steam_id_64(self):
        self.assertEqual(parse_friend_code(" 123456789 "), ACCOUNT)
        self.assertEqual(parse_friend_code(str(STEAM_ID_BASE + ACCOUNT)), ACCOUNT)
        self.assertEqual(parse_friend_code("4294967295"), 2**32 - 1)

    def test_invalid_friend_codes(self):
        for value in ("", "0", "-1", "1.5", "ABC", "１２３", "4294967296", str(STEAM_ID_BASE)):
            with self.subTest(value=value), self.assertRaises(UserError):
                parse_friend_code(value)

    def test_requested_match_count_defaults_and_validation(self):
        self.assertEqual(DEFAULT_MATCH_COUNT, 20)
        self.assertEqual(MAX_MATCH_COUNT, 100)
        self.assertEqual(PlayerHistory(ACCOUNT, "旧构造兼容", ()).requested_count, 20)
        for count in (1, 5, 20, 100):
            with self.subTest(count=count):
                self.assertEqual(validate_match_count(count), count)
        for count in (0, -1, 101, True, False, 5.0, "5", None):
            with self.subTest(count=count), self.assertRaises(UserError):
                validate_match_count(count)

    def test_average_excludes_missing_but_includes_zero(self):
        summary = summarize(history([None, 0.0, -9.0, 3.0]))
        self.assertEqual(summary.average, -2.0)
        self.assertEqual(summary.valid_count, 3)
        self.assertEqual(summary.missing_count, 1)

    def test_no_values_cannot_rate(self):
        for scores in ([], [None, None]):
            with self.subTest(scores=scores):
                summary = summarize(history(scores))
                self.assertIsNone(summary.average)
                self.assertEqual(summary.valid_count, 0)

    def test_placeholder_expansion_is_checked_after_rendering(self):
        source = PlayerHistory(ACCOUNT, "测" * 100, (Match(1, 100, 2),))
        summary = summarize(source)
        self.assertEqual(len(render_text(summary, Rule(None, "test", "{name}" * 35, None))), 3500)
        with self.assertRaises(ConfigError):
            render_text(summary, Rule(None, "test", "{name}" * 36, None))

    def test_boundaries_are_lower_inclusive_and_unrounded(self):
        rules = (Rule(None, "low", "x", None), Rule(-10, "mid", "x", None), Rule(0, "high", "x", None))
        for score, title in ((-20, "low"), (-10.001, "low"), (-10, "mid"),
                             (-0.001, "mid"), (0, "high"), (100, "high")):
            with self.subTest(score=score):
                self.assertEqual(select_rule(rules, score).title, title)
        self.assertEqual(format_score(-0.001), "+0.00")
        exclusive_rules = (
            Rule(None, "low", "x", None),
            Rule(-10, "mid", "x", None, include_min=False),
            Rule(0, "high", "x", None, include_min=False),
        )
        for score, title in ((-10, "low"), (-9.999, "mid"),
                             (0, "mid"), (0.001, "high")):
            with self.subTest(exclusive_score=score):
                self.assertEqual(select_rule(exclusive_rules, score).title, title)

    def test_default_rules_have_expected_boundaries(self):
        rules = load_rules(ROOT / "config.json")
        self.assertEqual([rule.minimum for rule in rules], [None, -10, 0, 10])
        self.assertEqual([rule.include_min for rule in rules], [True, False, True, False])
        self.assertTrue(all(rule.text == REPLY_TEXT for rule in rules))
        images = (
            "assets/苯猪_1.webp", "assets/猪bee.webp",
            "assets/不赖猪.jpg", "assets/好猪好猪.webp",
        )
        self.assertEqual(tuple(rule.image for rule in rules), images)
        self.assertEqual(load_unavailable(ROOT / "config.json"),
                         ("神秘嘉宾来袭", "assets/抱猪_1.jpg"))
        for score, index in ((-100, 0), (-10.001, 0), (-10, 0),
                             (-9.999, 1), (-0.001, 1),
                             (0, 2), (0.001, 2), (9.999, 2), (10, 2),
                             (10.001, 3), (100, 3)):
            with self.subTest(score=score):
                self.assertEqual(select_rule(rules, score).image, images[index])

    def test_bad_configuration_fails_early(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mutate in (
                lambda config: config["rules"][0].update(min=-100),
                lambda config: config["rules"][1].update(min=True),
                lambda config: config["rules"][1].update(min=math.inf),
                lambda config: config["rules"][1].update(include_min=1),
                lambda config: config["rules"][1].update(include_min="false"),
                lambda config: config["rules"][1].update(include_min=None),
                lambda config: config["rules"][0].update(text="{average:.2f}"),
                lambda config: config["rules"][0].update(text="{name.__class__}"),
                lambda config: config["rules"][0].update(image="../outside.png"),
                lambda config: config["rules"][0].update(image="https://user:password@example.com/pic.png"),
            ):
                config = write_config(root)
                mutate(config)
                (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(config=config), self.assertRaises(ConfigError):
                    load_rules(root / "config.json")

    def test_unavailable_configuration_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = write_config(root)
            del config["unavailable"]
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(load_unavailable(root / "config.json"), ("神秘嘉宾来袭", None))
            for unavailable in (
                "invalid", {"text": "", "image": None},
                {"text": "神秘嘉宾来袭", "image": "../outside.jpg"},
                {"text": "神秘嘉宾来袭", "image": "https://user:password@example.com/pic.jpg"},
            ):
                config = write_config(root)
                config["unavailable"] = unavailable
                (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(unavailable=unavailable), self.assertRaises(ConfigError):
                    load_unavailable(root / "config.json")


class ParseHistoryTests(unittest.TestCase):
    def test_default_twenty_and_explicit_match_window(self):
        records = [record(i, i * 100, float(i)) for i in range(1, 26)]
        default = parse_history(payload(records), ACCOUNT)
        self.assertEqual(default.requested_count, 20)
        self.assertEqual([match.id for match in default.matches], list(range(25, 5, -1)))
        self.assertEqual(summarize(default).average, 15.5)
        explicit = parse_history(payload(records), ACCOUNT, match_count=3)
        self.assertEqual(explicit.requested_count, 3)
        self.assertEqual([match.id for match in explicit.matches], [25, 24, 23])
        self.assertEqual(summarize(explicit).average, 24.0)

    def test_only_target_player_imp_is_used(self):
        result = parse_history(payload([record(1, 100, -4)]), ACCOUNT)
        self.assertEqual(result.matches[0].imp, -4)

    def test_missing_target_and_missing_imp_are_missing(self):
        result = parse_history(payload([record(1, 100, None), record(2, 200, 888, target=42)]), ACCOUNT)
        self.assertEqual([match.imp for match in result.matches], [None, None])
        self.assertIsNone(summarize(result).average)

    def test_raw_page_parser_sorts_and_preserves_missing_scores(self):
        records = [record(i, i * 100, None if i == 25 else float(i)) for i in range(1, 26)]
        result = parse_history(payload(records), ACCOUNT, match_count=20)
        self.assertEqual([match.id for match in result.matches], list(range(25, 5, -1)))
        self.assertEqual(result.requested_count, 20)
        summary = summarize(result)
        self.assertEqual(summary.valid_count, 19)
        self.assertEqual(summary.missing_count, 1)
        self.assertEqual(summary.average, 15.0)  # Raw-page parsing keeps ID 25 without an IMP.

    def test_valid_selection_skips_missing_keeps_zero_and_deduplicates(self):
        scores = {10: None, 9: 0, 8: None, 7: -5, 6: 20, 5: None,
                  4: 40, 3: 50, 2: -1000, 1: None}
        records = [record(i, i * 100, scores[i]) for i in range(1, 11)]
        records.append(record(7, 700, -5))
        result = parse_valid_history(payload(records), ACCOUNT, match_count=5)
        self.assertEqual([match.id for match in result.matches], [9, 7, 6, 4, 3])
        self.assertEqual([match.imp for match in result.matches], [0, -5, 20, 40, 50])
        self.assertEqual(result.requested_count, 5)
        summary = summarize(result)
        self.assertEqual(summary.average, 21.0)
        self.assertEqual(summary.valid_count, 5)
        self.assertEqual(summary.missing_count, 0)
        embed, attachment = make_embed(summary, ROOT)
        self.assertEqual(embed.description, "目标玩家近期平均IMP为：+21.00\n一眼盯真 鉴定为")
        self.assertEqual(attachment, ROOT / "assets" / "好猪好猪.webp")
        self.assertFalse(embed.footer.text)
        fewer = parse_valid_history(payload(records), ACCOUNT, match_count=2)
        self.assertEqual([match.id for match in fewer.matches], [9, 7])
        self.assertEqual(fewer.requested_count, 2)
        self.assertEqual(summarize(fewer).average, -2.5)

    def test_five_valid_scores_select_expected_benzhu_image(self):
        scores = [5, -22, -28, -11, 3]
        records = [record(100 - index, 1000 - index, score) for index, score in enumerate(scores)]
        result = parse_valid_history(payload(records), ACCOUNT, match_count=5)
        summary = summarize(result)
        self.assertEqual([match.imp for match in result.matches], scores)
        self.assertEqual(summary.average, -10.6)
        embed, attachment = make_embed(summary, ROOT)
        self.assertEqual(embed.description, "目标玩家近期平均IMP为：-10.60\n一眼盯真 鉴定为")
        self.assertEqual(attachment, ROOT / "assets" / "苯猪_1.webp")

    def test_fewer_valid_scores_or_no_valid_scores_keep_requested_count(self):
        for scores in ([None, 0, None, 8, None], [None] * 5, []):
            with self.subTest(scores=scores):
                records = [record(100 - index, 1000 - index, score) for index, score in enumerate(scores)]
                result = parse_valid_history(payload(records), ACCOUNT, match_count=5)
                self.assertEqual(result.requested_count, 5)
                self.assertTrue(all(match.imp is not None for match in result.matches))
                summary = summarize(result)
                embed, attachment = make_embed(summary, ROOT)
                if any(score is not None for score in scores):
                    self.assertEqual(len(result.matches), 2)
                    self.assertEqual(summary.average, 4.0)
                    self.assertIn("2", embed.footer.text)
                else:
                    self.assertEqual(result.matches, ())
                    self.assertIsNone(summary.average)
                    self.assertEqual(embed.description, "神秘嘉宾来袭")
                    self.assertEqual(attachment, ROOT / "assets" / "抱猪_1.jpg")
                    self.assertFalse(embed.footer.text)

    def test_duplicate_match_ids_are_counted_once(self):
        result = parse_history(payload([record(1, 100, 4), record(1, 100, 4), record(2, 200, -2)]), ACCOUNT)
        self.assertEqual(len(result.matches), 2)
        self.assertEqual(summarize(result).average, 1.0)

    def test_api_integer_strings_are_supported(self):
        row = record("101", "1700000000", 12)
        row["players"][1]["steamAccountId"] = str(ACCOUNT)
        result = parse_history(payload([row]), ACCOUNT)
        self.assertEqual(result.matches[0], Match(101, 1700000000, 12.0))

    def test_invalid_scores_cannot_be_silently_averaged(self):
        for score in (True, "4", math.inf, -math.inf, math.nan):
            with self.subTest(score=score), self.assertRaises(UserError):
                parse_history(payload([record(1, 100, score)]), ACCOUNT)

    def test_bad_data_fails_safely(self):
        values = [{}, {"data": {}}, {"data": {"player": {}}},
                  {"data": {"player": []}}, {"data": {"player": {"matches": "hidden"}}}, payload([{}]),
                  payload([{**record(1, 100), "players": None}])]
        for value in values:
            with self.subTest(payload=value), self.assertRaises(UserError):
                parse_history(value, ACCOUNT)

    def test_null_player_or_explicit_null_matches_is_unavailable_history(self):
        values = [{"data": {"player": None}},
                  {"data": {"player": {"steamAccount": {"name": "隐藏玩家"}, "matches": None}}}]
        for value in values:
            for count in (5, 20):
                with self.subTest(payload=value, count=count):
                    result = parse_history(value, ACCOUNT, match_count=count)
                    self.assertEqual(result.account_id, ACCOUNT)
                    self.assertEqual(result.matches, ())
                    self.assertEqual(result.requested_count, count)
                    self.assertIsNone(summarize(result).average)


class FakeResponse:
    def __init__(self, status=200, data=None, error=None):
        self.status, self.data, self.error = status, data, error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        if self.error:
            raise self.error
        return self.data


class StratzClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_uses_latest_requested_valid_scores_including_zero(self):
        scores = [None, 0, None, 5, None, -5, 10, 20, -1000] + [1] * 16
        records = [record(100 - index, 1000 - index, score) for index, score in enumerate(scores)]
        for selected_count in (5, None):
            with self.subTest(selected_count=selected_count):
                session = Mock()
                session.post.return_value = FakeResponse(data=payload(records))
                client = StratzClient(session, "placeholder")
                if selected_count is None:
                    result = await client.fetch_history(ACCOUNT)
                    expected_scores = [0, 5, -5, 10, 20, -1000] + [1] * 14
                    expected_count, expected_average = 20, -47.8
                else:
                    result = await client.fetch_history(ACCOUNT, selected_count)
                    expected_scores = [0, 5, -5, 10, 20]
                    expected_count, expected_average = 5, 6.0
                self.assertEqual([match.imp for match in result.matches], expected_scores)
                self.assertEqual(result.requested_count, expected_count)
                self.assertEqual(summarize(result).average, expected_average)
                self.assertEqual(summarize(result).valid_count, expected_count)
                session.post.assert_called_once()

    async def test_request_uses_default_or_selected_window_and_required_headers(self):
        session = Mock()
        session.post.return_value = FakeResponse(data=payload([]))
        client = StratzClient(session, "Bearer placeholder")
        for selected_count in (None, 3, 100):
            with self.subTest(selected_count=selected_count):
                if selected_count is None:
                    result = await client.fetch_history(ACCOUNT)
                else:
                    result = await client.fetch_history(ACCOUNT, selected_count)
                count = selected_count if selected_count is not None else 20
                kwargs = session.post.call_args.kwargs
                self.assertIn("take: $take", QUERY)
                self.assertIn("orderBy: DESC", QUERY)
                self.assertIn("minImp: -32768", QUERY)
                self.assertEqual(kwargs["json"]["variables"], {"steamAccountId": ACCOUNT, "take": count})
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer placeholder")
                self.assertEqual(kwargs["headers"]["User-Agent"], "STRATZ_API")
                self.assertFalse(kwargs["allow_redirects"])
                self.assertEqual(result.requested_count, count)

    async def test_http_failures_have_safe_messages(self):
        for status in (401, 403, 429, 302, 500):
            with self.subTest(status=status):
                session = Mock()
                session.post.return_value = FakeResponse(status=status)
                with self.assertRaises(UserError):
                    await StratzClient(session, "placeholder").fetch_history(ACCOUNT)

    async def test_graphql_errors_reject_partial_data(self):
        data = payload([record(1, 100, 25)])
        data["errors"] = [{"message": "do not expose this raw error"}]
        session = Mock()
        session.post.return_value = FakeResponse(data=data)
        with self.assertRaises(UserError) as raised:
            await StratzClient(session, "placeholder").fetch_history(ACCOUNT)
        self.assertNotIn("do not expose", str(raised.exception))

    async def test_network_and_timeout_and_json_failures(self):
        for error in (asyncio.TimeoutError(), aiohttp.ClientConnectionError()):
            with self.subTest(error=error):
                session = Mock()
                session.post.side_effect = error
                with self.assertRaises(UserError):
                    await StratzClient(session, "placeholder").fetch_history(ACCOUNT)
        session = Mock()
        session.post.return_value = FakeResponse(error=ValueError("invalid json"))
        with self.assertRaises(UserError):
            await StratzClient(session, "placeholder").fetch_history(ACCOUNT)

    async def test_nonobject_json_is_rejected(self):
        for data in (None, [], "unexpected"):
            with self.subTest(data=data):
                session = Mock()
                session.post.return_value = FakeResponse(data=data)
                with self.assertRaises(UserError):
                    await StratzClient(session, "placeholder").fetch_history(ACCOUNT)

    async def test_cache_and_concurrent_queries_share_one_request(self):
        client = StratzClient(Mock(), "placeholder")
        gate = asyncio.Event()

        async def fetch(account_id, match_count):
            await gate.wait()
            return payload([record(1, 100, 3)])

        client._request = AsyncMock(side_effect=fetch)
        first = asyncio.create_task(client.fetch_history(ACCOUNT))
        second = asyncio.create_task(client.fetch_history(ACCOUNT))
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(results[0], results[1])
        self.assertEqual(await client.fetch_history(ACCOUNT), results[0])
        client._request.assert_awaited_once_with(ACCOUNT, 20)
        self.assertEqual(client._pending, {})

    async def test_different_match_windows_do_not_share_cache_or_pending_results(self):
        client = StratzClient(Mock(), "placeholder")
        gate = asyncio.Event()

        async def fetch(account_id, match_count):
            await gate.wait()
            return payload([record(i, i * 100, float(i)) for i in range(1, 21)])

        client._request = AsyncMock(side_effect=fetch)
        first = asyncio.create_task(client.fetch_history(ACCOUNT, 5))
        second = asyncio.create_task(client.fetch_history(ACCOUNT, 20))
        await asyncio.sleep(0)
        self.assertEqual(set(client._pending), {(ACCOUNT, 5), (ACCOUNT, 20)})
        gate.set()
        five, twenty = await asyncio.gather(first, second)
        self.assertEqual(len(five.matches), 5)
        self.assertEqual(len(twenty.matches), 20)
        self.assertNotEqual(summarize(five).average, summarize(twenty).average)
        self.assertEqual(await client.fetch_history(ACCOUNT, 5), five)
        self.assertEqual(await client.fetch_history(ACCOUNT, 20), twenty)
        client._request.assert_has_awaits([call(ACCOUNT, 5), call(ACCOUNT, 20)], any_order=True)
        self.assertEqual(client._request.await_count, 2)


class EmbedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        write_config(self.root)

    def test_fewer_valid_scores_keep_fixed_reply_and_coverage_footer(self):
        embed, attachment = make_embed(summarize(history([1.0] * 3, requested_count=5)), self.root)
        self.assertIsNone(attachment)
        self.assertEqual(embed.description, "目标玩家近期平均IMP为：+1.00\n一眼盯真 鉴定为")
        self.assertFalse(embed.title)
        self.assertEqual(embed.fields, [])
        self.assertTrue(embed.footer.text)
        self.assertIn("3", embed.footer.text)

    def test_no_scores_produce_unavailable_reply_and_its_image(self):
        write_config(self.root, image="https://example.com/rating.png",
                     unavailable_image="https://example.com/mystery.jpg")
        for scores in ([], [None, None]):
            with self.subTest(scores=scores):
                embed, attachment = make_embed(summarize(history(scores)), self.root)
                self.assertIsNone(attachment)
                self.assertEqual(embed.description, "神秘嘉宾来袭")
                self.assertEqual(embed.image.url, "https://example.com/mystery.jpg")
                self.assertFalse(embed.title)
                self.assertEqual(embed.fields, [])
                self.assertFalse(embed.footer.text)

    def test_local_image_uses_matching_attachment_name(self):
        (self.root / "assets").mkdir()
        image = self.root / "assets" / "rating.PNG"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        write_config(self.root, image="assets/rating.PNG")
        embed, attachment = make_embed(summarize(history([5])), self.root)
        self.assertEqual(attachment, image)
        self.assertEqual(embed.image.url, "attachment://rating.png")

    def test_remote_image_requires_no_file(self):
        write_config(self.root, image="https://example.com/image.png")
        embed, attachment = make_embed(summarize(history([5])), self.root)
        self.assertIsNone(attachment)
        self.assertEqual(embed.image.url, "https://example.com/image.png")

    def test_complete_requested_scores_show_only_fixed_reply(self):
        for count in (5, 20):
            for score in (-5.0, 5.0):
                with self.subTest(count=count, score=score):
                    embed, _ = make_embed(summarize(history([score] * count)), self.root)
                    self.assertEqual(embed.description, REPLY_TEXT.format(average=format_score(score)))
                    self.assertFalse(embed.title)
                    self.assertEqual(embed.fields, [])
                    self.assertFalse(embed.footer.text)


class DiscordCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        write_config(self.root)
        self.client = ImpClient(self.root, "placeholder", 60)
        self.client.stratz = SimpleNamespace(fetch_history=AsyncMock(return_value=history([10] * 20)))
        self.command = self.client.get_command(COMMAND_NAME)
        self.ctx = SimpleNamespace(
            author=SimpleNamespace(id=42),
            guild=None,
            reply=AsyncMock(),
        )

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_exact_prefix_command_names_and_parameters(self):
        self.assertEqual(self.client.command_prefix, COMMAND_PREFIX)
        self.assertEqual(COMMAND_PREFIX, "!")
        self.assertTrue(self.client.intents.message_content)
        for name in ("查询受教育程度", "查询"):
            with self.subTest(name=name):
                command = self.client.get_command(name)
                self.assertIsNotNone(command)
                self.assertIs(command, self.command)
                friend_code, match_count = command.clean_params.values()
                self.assertEqual(friend_code.name, "friend_code")
                self.assertEqual(friend_code.annotation, str)
                self.assertEqual(match_count.name, "match_count")
                self.assertEqual(match_count.annotation, int)
                self.assertEqual(match_count.default, 20)

    async def test_both_commands_share_callback_and_user_cooldown(self):
        alias = self.client.get_command("查询")
        self.assertIs(alias, self.command)
        cooldown = self.command._buckets._cooldown
        self.assertEqual(cooldown.rate, 1)
        self.assertEqual(cooldown.per, 10)
        self.assertEqual(self.command._buckets._type, commands.BucketType.user)

    async def test_invalid_code_replies_and_does_not_fetch(self):
        await self.command.callback(self.ctx, "invalid")
        self.ctx.reply.assert_awaited_once()
        self.assertTrue(self.ctx.reply.call_args.args[0])
        self.assertFalse(self.ctx.reply.call_args.kwargs["mention_author"])
        self.client.stratz.fetch_history.assert_not_awaited()

    async def test_success_sends_embed(self):
        await self.command.callback(self.ctx, str(ACCOUNT))
        self.client.stratz.fetch_history.assert_awaited_once_with(ACCOUNT, 20)
        sent = self.ctx.reply.call_args.kwargs
        self.assertIsInstance(sent["embed"], discord.Embed)
        self.assertNotIn("file", sent)
        self.assertFalse(sent["mention_author"])

    async def test_both_commands_pass_explicit_selected_match_count(self):
        for name, count in (("查询受教育程度", 5), ("查询", 3)):
            with self.subTest(name=name, count=count):
                self.client.stratz.fetch_history.reset_mock()
                self.client.stratz.fetch_history.return_value = history([10] * count)
                command = self.client.get_command(name)
                await command.callback(self.ctx, str(ACCOUNT), count)
                self.client.stratz.fetch_history.assert_awaited_once_with(ACCOUNT, count)

    async def test_command_complete_history_keeps_fixed_reply_without_cache_note(self):
        self.client.cache_ttl = 300
        self.client.stratz.fetch_history.return_value = history([10] * 20)
        await self.command.callback(self.ctx, str(ACCOUNT))
        embed = self.ctx.reply.call_args.kwargs["embed"]
        self.assertEqual(embed.description, "目标玩家近期平均IMP为：+10.00\n一眼盯真 鉴定为")
        self.assertFalse(embed.title)
        self.assertEqual(embed.fields, [])
        self.assertFalse(embed.footer.text)

    async def test_stratz_error_is_reported(self):
        self.client.stratz.fetch_history.side_effect = UserError("公开数据不可用")
        await self.command.callback(self.ctx, str(ACCOUNT))
        self.ctx.reply.assert_awaited_once_with("公开数据不可用", mention_author=False)

    async def test_null_player_uses_mystery_reply_and_local_attachment(self):
        image = self.root / "mystery.jpg"
        image.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
        write_config(self.root, unavailable_image="mystery.jpg")
        self.client.stratz.fetch_history.return_value = parse_history({"data": {"player": None}}, ACCOUNT)
        await self.command.callback(self.ctx, str(ACCOUNT))
        sent = self.ctx.reply.call_args.kwargs
        self.assertEqual(sent["embed"].description, "神秘嘉宾来袭")
        self.assertEqual(sent["embed"].image.url, f"attachment://{sent['file'].filename}")
        self.assertFalse(sent["embed"].title)
        self.assertEqual(sent["embed"].fields, [])
        self.assertFalse(sent["embed"].footer.text)
        self.assertTrue(sent["file"].fp.closed)

    async def test_http_failure_reports_error_instead_of_mystery_reply(self):
        session = Mock()
        session.post.return_value = FakeResponse(status=401)
        self.client.stratz = StratzClient(session, "placeholder")
        await self.command.callback(self.ctx, str(ACCOUNT))
        self.ctx.reply.assert_awaited_once()
        sent = self.ctx.reply.call_args
        self.assertIn("401", sent.args[0])
        self.assertFalse(sent.kwargs["mention_author"])
        self.assertNotIn("神秘嘉宾", sent.args[0])
        self.assertNotIn("embed", sent.kwargs)

    async def test_bad_rule_config_is_reported(self):
        (self.root / "config.json").write_text("{broken", encoding="utf-8")
        await self.command.callback(self.ctx, str(ACCOUNT))
        self.ctx.reply.assert_awaited_once_with(
            "评价或图片配置有误，请让管理员检查 config.json。", mention_author=False)

    async def test_local_file_is_sent_with_embed_reference(self):
        image = self.root / "rating.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        write_config(self.root, image="rating.png")
        await self.command.callback(self.ctx, str(ACCOUNT))
        sent = self.ctx.reply.call_args.kwargs
        self.assertEqual(sent["file"].filename, "rating.png")
        self.assertEqual(sent["embed"].image.url, "attachment://rating.png")
        self.assertTrue(sent["file"].fp.closed)

    async def test_failed_image_upload_falls_back_to_text(self):
        (self.root / "rating.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        write_config(self.root, image="rating.png")
        error = discord.HTTPException(SimpleNamespace(status=413, reason="Too large"), "upload failed")
        self.ctx.reply.side_effect = [error, None]
        await self.command.callback(self.ctx, str(ACCOUNT))
        self.assertEqual(self.ctx.reply.await_count, 2)
        fallback = self.ctx.reply.call_args.kwargs
        self.assertIn("图片暂时发送失败", fallback["content"])
        self.assertFalse(fallback["embed"].image.url)
        self.assertNotIn("file", fallback)
        failed_attachment = self.ctx.reply.call_args_list[0].kwargs["file"]
        self.assertTrue(failed_attachment.fp.closed)

    async def test_reboot_is_registered_and_restricted_to_admin(self):
        reboot = self.client.get_command("reboot")
        self.assertIsNotNone(reboot)
        denied = SimpleNamespace(author=SimpleNamespace(id=42), reply=AsyncMock())
        await reboot.callback(denied)
        denied.reply.assert_not_awaited()

        allowed = SimpleNamespace(author=SimpleNamespace(id=REBOOT_ADMIN_USER_ID), reply=AsyncMock())
        with patch("impbot.discord_app.asyncio.sleep", new=AsyncMock()) as sleep, \
                patch("impbot.discord_app.os._exit", new=Mock()) as exit_process:
            await reboot.callback(allowed)
            allowed.reply.assert_awaited_once_with("正在拉取更新并重启机器人。", mention_author=False)
            sleep.assert_awaited_once_with(1)
            exit_process.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
