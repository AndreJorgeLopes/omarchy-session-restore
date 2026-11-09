"""Final ordering pass, idempotent name placement, Chrome New Tab stand-in,
and OpenCode session ids.

The restore cases come from a real log (2026-11-09): a Discord splash window
was placed and then closed, so workspace 1 was never ordered; dwindle stacks
put every window of a workspace in one column, so ordering by x alone did
nothing; two Zen windows and four foot terminals all share a creation name
("Zen Browser", "foot"), so the second name pass swapped them around.

Hyprland is faked: ``clients`` is a list of dicts and ``dispatch`` applies
window.move / window.swap to it.
"""

import importlib.machinery
import importlib.util
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BIN = os.path.join(_ROOT, "bin", "omarchy-session")
_loader = importlib.machinery.SourceFileLoader("omarchy_session_order", _BIN)
_spec = importlib.util.spec_from_loader("omarchy_session_order", _loader)
m = importlib.util.module_from_spec(_spec)
_loader.exec_module(m)

_ADDR = re.compile(r"address:([^\]]+)\]")
_WS = re.compile(r"workspace = \[=*\[([^\]]+)\]")


class FakeDesk:
    def __init__(self, clients):
        self.clients = clients
        self.calls = []

    def by_addr(self, a):
        return next(c for c in self.clients if c["address"] == a)

    def dispatch(self, lua):
        self.calls.append(lua)
        addrs = _ADDR.findall(lua)
        if lua.startswith("hl.dsp.window.swap"):
            # target first, then window, as the plugin writes them
            a, b = self.by_addr(addrs[0]), self.by_addr(addrs[1])
            a["at"], b["at"] = b["at"], a["at"]
        elif lua.startswith("hl.dsp.window.move") and "workspace" in lua:
            ws = _WS.search(lua).group(1)
            self.by_addr(addrs[0])["workspace"] = {"id": int(ws), "name": ws}
        return True

    def swaps(self):
        return [c for c in self.calls if c.startswith("hl.dsp.window.swap")]

    def moves(self):
        return [c for c in self.calls if c.startswith("hl.dsp.window.move")]


def client(addr, cls, ws, at, title, initial=None):
    return {"address": addr, "class": cls, "initialClass": cls,
            "workspace": {"id": ws, "name": str(ws)}, "at": list(at),
            "title": title, "initialTitle": initial if initial is not None else title,
            "floating": False, "pid": 1}


def saved(ws, at, title, initial):
    return {"workspace": ws, "ws_name": str(ws), "at": list(at), "size": [100, 100],
            "floating": False, "title": title, "initial_title": initial, "xdg_tag": ""}


def make_restorer(desk, sess, cfg=None):
    r = m.Restorer.__new__(m.Restorer)
    r.cfg = dict(m.DEFAULTS, **(cfg or {}))
    r.use_uwsm = False
    r.known, r.claimed, r.pending, r.failed = set(), set(), [], []
    r.placed, r.placed_at, r.placed_src, r._late = 0, {}, {}, []
    r._prof_cache, r.hooked, r.refocus, r.prev_focus = {}, False, None, None
    r.sess, r._name_tried, r.monitors_now = sess, set(), []
    r.clients = lambda: desk.clients
    r._layout = lambda ws_id: "dwindle"
    r._profile_of_client = lambda c: ""
    return r


class FakeDispatchTestCase(unittest.TestCase):
    def use(self, desk):
        orig = m.dispatch
        m.dispatch = desk.dispatch
        self.addCleanup(setattr, m, "dispatch", orig)
        orig_sleep = m.time.sleep
        m.time.sleep = lambda s: None
        self.addCleanup(setattr, m.time, "sleep", orig_sleep)


class ReadingOrderTests(unittest.TestCase):
    def test_stacked_dwindle_column_is_ordered_top_to_bottom(self):
        pos = {"discord": (1489, 1482), "whatsapp": (1489, 638)}
        self.assertEqual(m.reading_order(pos), ["whatsapp", "discord"])

    def test_columns_then_rows(self):
        pos = {"zen-nos": (3998, 2102), "foot": (2569, 1118), "zen-yt": (3998, 1118),
               "chromium": (2569, 2027)}
        self.assertEqual(m.reading_order(pos), ["foot", "chromium", "zen-yt", "zen-nos"])

    def test_slot_swaps_converge_on_a_single_column(self):
        # Workspace 6 as the user wants it, top to bottom, all in one column.
        desired = ["nos", "chrome", "zen-ali", "claude"]
        live = {"claude": (5129, 638), "zen-ali": (5129, 1100),
                "chrome": (5129, 1580), "nos": (5129, 2049)}
        swaps = 0
        while True:
            step = m.next_slot_swap(desired, live)
            if step is None:
                break
            want, here = step
            live[want], live[here] = live[here], live[want]
            swaps += 1
            self.assertLessEqual(swaps, len(desired) - 1)
        self.assertEqual(m.reading_order(live), desired)

    def test_nothing_to_do_when_already_in_order(self):
        live = {"a": (0, 0), "b": (0, 500), "c": (900, 0)}
        self.assertIsNone(m.next_slot_swap(["a", "b", "c"], live))


class FinalOrderTests(FakeDispatchTestCase):
    def test_workspace_with_a_replaced_splash_window_is_still_ordered(self):
        # Discord's splash (0xsplash) was the window place() saw. The real
        # Discord window came later on its own, and WhatsApp arrived late.
        w_discord = saved(1, (1489, 1482), "General - Discord", "Discord")
        w_whatsapp = saved(1, (1489, 638), "web.whatsapp.com", "web.whatsapp.com_/")
        sess = {"items": [
            {"kind": "app", "class": "discord-ptb", "windows": [w_discord]},
            {"kind": "webapp", "class": "chrome-web.whatsapp.com__-Default", "windows": [w_whatsapp]},
        ]}
        desk = FakeDesk([
            client("0xdiscord", "discord-ptb", 1, (1489, 638), "General - Discord", "Discord"),
            client("0xwa", "chrome-web.whatsapp.com__-Default", 1, (1489, 1482),
                   "web.whatsapp.com", "web.whatsapp.com_/"),
        ])
        self.use(desk)
        r = make_restorer(desk, sess)
        r.placed_at["0xsplash"] = w_discord
        r.placed_src["0xsplash"] = (id(w_discord), "discord-ptb")
        r.reorder_tiled(sess)
        self.assertEqual(len(desk.swaps()), 1)
        self.assertEqual(desk.by_addr("0xwa")["at"], [1489, 638])
        self.assertEqual(desk.by_addr("0xdiscord")["at"], [1489, 1482])

    def test_windows_already_in_place_are_ordered_too(self):
        # Nothing was moved by the restorer (placed_at is empty).
        w_a = saved(6, (5129, 638), "NOS TV — Zen Browser", "Zen Browser")
        w_b = saved(6, (5129, 1580), "AliExpress - Chromium", "AliExpress - Chromium")
        sess = {"items": [
            {"kind": "browser", "class": "zen", "windows": [w_a]},
            {"kind": "browser", "class": "chromium", "windows": [w_b]},
        ]}
        desk = FakeDesk([
            client("0xzen", "zen", 6, (5129, 1580), "NOS TV — Zen Browser", "Zen Browser"),
            client("0xchr", "chromium", 6, (5129, 638), "AliExpress - Chromium"),
        ])
        self.use(desk)
        r = make_restorer(desk, sess)
        r.reorder_tiled(sess)
        self.assertEqual(desk.by_addr("0xzen")["at"], [5129, 638])

    def test_ordering_is_skipped_when_in_order(self):
        w_a = saved(2, (0, 0), "one", "one")
        w_b = saved(2, (0, 500), "two", "two")
        sess = {"items": [{"kind": "app", "class": "x", "windows": [w_a, w_b]}]}
        desk = FakeDesk([client("0xa", "x", 2, (0, 0), "one"), client("0xb", "x", 2, (0, 500), "two")])
        self.use(desk)
        make_restorer(desk, sess).reorder_tiled(sess)
        self.assertEqual(desk.swaps(), [])


class NamePassTests(FakeDispatchTestCase):
    def test_shared_creation_name_does_not_pair_look_alike_windows(self):
        saved_rows = [
            {"class": "zen", "profile": "", "title": "NOS TV — Zen Browser",
             "initial_title": "Zen Browser", "xdg_tag": ""},
            {"class": "zen", "profile": "", "title": "Weidian Search: quad | JadeShip.com — Zen Browser",
             "initial_title": "Zen Browser", "xdg_tag": ""},
        ]
        live = [
            {"class": "zen", "profile": "", "title": "Weidian Search: quad (3 results) | JadeShip.com — Zen Browser",
             "initialTitle": "Zen Browser", "xdgTag": ""},
            {"class": "zen", "profile": "", "title": "NOS TV — Zen Browser",
             "initialTitle": "Zen Browser", "xdgTag": ""},
        ]
        pairs = sorted((si, ci) for si, ci, _s in m.match_windows_by_name(saved_rows, live))
        self.assertEqual(pairs, [(0, 1), (1, 0)])

    def test_unique_creation_name_still_counts(self):
        saved_rows = [{"class": "chromium", "profile": "", "title": "Something else entirely",
                       "initial_title": "feat(core): run Claude Code tool hooks", "xdg_tag": ""}]
        live = [{"class": "chromium", "profile": "", "title": "Loading…",
                 "initialTitle": "feat(core): run Claude Code tool hooks", "xdgTag": ""},
                {"class": "chromium", "profile": "", "title": "Other",
                 "initialTitle": "Other", "xdgTag": ""}]
        pairs = m.match_windows_by_name(saved_rows, live)
        self.assertEqual([(si, ci) for si, ci, _s in pairs], [(0, 0)])

    def test_second_pass_does_not_move_windows_already_placed(self):
        # From the log: four foot terminals all created as "foot". The first
        # pass put each one on its workspace; the name pass then moved them
        # 2 -> 6, 2 -> 11, 6 -> 2 and 11 -> 2.
        ws = [(6, "OC | Agentic coding harness comparison"), (11, "cigano@arch-cigano:~"),
              (2, "✳ Agent-setup and OpenCode migration"), (2, "cigano@arch-cigano:~")]
        wins = [saved(w, (0, i * 100), t, "foot") for i, (w, t) in enumerate(ws)]
        sess = {"items": [{"kind": "terminal", "class": "foot", "windows": [w]} for w in wins]}
        # Live windows listed in a different order than saved, each already
        # on its saved workspace (live i holds saved order[i]).
        order = [2, 3, 0, 1]
        desk = FakeDesk([client(f"0x{i}", "foot", ws[j][0], (0, j * 100), ws[j][1], "foot")
                         for i, j in enumerate(order)])
        self.use(desk)
        r = make_restorer(desk, sess)
        for i, j in enumerate(order):
            r.placed_at[f"0x{i}"] = wins[j]
            r.placed_src[f"0x{i}"] = (id(wins[j]), "foot")
        r.place_named_windows()
        self.assertEqual(desk.moves(), [])

    def test_reused_address_of_a_closed_splash_is_placed(self):
        # Steam's splash was placed on 5 and closed; WhatsApp got its address.
        w_steam = saved(5, (0, 0), "Steam", "Steam")
        w_wa = saved(1, (0, 0), "web.whatsapp.com", "web.whatsapp.com_/")
        sess = {"items": [{"kind": "app", "class": "steam", "windows": [w_steam]},
                          {"kind": "webapp", "class": "chrome-web.whatsapp.com__-Default",
                           "windows": [w_wa]}]}
        desk = FakeDesk([client("0xre", "chrome-web.whatsapp.com__-Default", 5, (0, 0),
                                "web.whatsapp.com", "web.whatsapp.com_/")])
        self.use(desk)
        r = make_restorer(desk, sess)
        r.placed_at["0xre"] = w_steam
        r.placed_src["0xre"] = (id(w_steam), "steam")
        r.failed.append("chrome-web.whatsapp.com__-Default")
        r._late.append("chrome-web.whatsapp.com__-Default")
        self.assertEqual(r.place_named_windows(), 1)
        self.assertEqual(desk.by_addr("0xre")["workspace"]["id"], 1)
        # It arrived after all, so it is not reported as a problem.
        self.assertEqual(r.failed, [])


class WebappLaunchTests(FakeDispatchTestCase):
    def test_webapps_launch_before_apps_and_terminals(self):
        sess = {"items": [
            {"kind": "webapp", "class": "chrome-web.whatsapp.com__-Default",
             "windows": [saved(1, (0, 0), "web.whatsapp.com", "")]},
            {"kind": "app", "class": "steam", "windows": [saved(5, (0, 0), "Steam", "")]},
            {"kind": "terminal", "class": "foot", "windows": [saved(2, (0, 0), "t", "")]},
            {"kind": "browser", "class": "chromium", "windows": [saved(6, (0, 0), "b", "")]},
        ]}
        kinds = [it["kind"] for it in m.ordered_items(sess)]
        # Apps and terminals keep their workspace order (foot on 2, Steam on 5).
        self.assertEqual(kinds, ["browser", "webapp", "terminal", "app"])

    def test_window_on_a_reused_claimed_address_is_seen_by_the_launch_wait(self):
        desk = FakeDesk([client("0xre", "chrome-web.whatsapp.com__-Default", 2, (0, 0),
                                "web.whatsapp.com")])
        self.use(desk)
        r = make_restorer(desk, {"items": []})
        r.claimed.add("0xre")
        r.placed_src["0xre"] = (0, "steam")
        got = r.unclaimed("chrome-web.whatsapp.com__-Default")
        self.assertEqual([c["address"] for c in got], ["0xre"])
        # A claimed window of the same class stays claimed.
        r.placed_src["0xre"] = (0, "chrome-web.whatsapp.com__-Default")
        self.assertEqual(r.unclaimed("chrome-web.whatsapp.com__-Default"), [])


class NtpStandinTests(FakeDispatchTestCase):
    def group(self, first_at, last_at):
        now = time.time()
        return {"class": "chromium", "kind": "browser", "profile": "Default",
                "windows": [saved(6, (0, 0), "AliExpress", "AliExpress")],
                "remaining": [saved(6, (0, 0), "AliExpress", "AliExpress")],
                "deadline": now + 120, "first_window_at": first_at,
                "last_group_change": last_at, "min_reopen_gap": 12, "reopen_after": now + 60}

    def test_new_tab_is_not_claimed_while_windows_may_still_arrive(self):
        desk = FakeDesk([client("0xntp", "chromium", 2, (0, 0), "New Tab - Chromium")])
        self.use(desk)
        r = make_restorer(desk, {"items": []})
        g = self.group(time.time() - 1, time.time() - 1)
        cands = [{"address": "0xntp", "title": "New Tab - Chromium", "raw": desk.clients[0]}]
        self.assertFalse(r._claim_ntp_standin(g, cands))
        self.assertIsNone(g.get("reopen_standin"))

    def test_new_tab_is_claimed_after_the_profile_settles(self):
        desk = FakeDesk([client("0xntp", "chromium", 2, (0, 0), "New Tab - Chromium")])
        self.use(desk)
        r = make_restorer(desk, {"items": []})
        g = self.group(time.time() - 60, time.time() - 30)
        cands = [{"address": "0xntp", "title": "New Tab - Chromium", "raw": desk.clients[0]}]
        self.assertTrue(r._claim_ntp_standin(g, cands))
        self.assertEqual(g["reopen_standin"], "0xntp")


class OpencodeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "opencode.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, directory TEXT)")
        con.executemany("INSERT INTO session_v2 VALUES (?, ?, ?)", [
            ("ses_one", "Omarchi workspace and hibernation issues", "/home/u"),
            ("ses_long", "Omarchy upgrade and configuration recovery", "/home/u"),
            ("ses_g1", "Greeting", "/home/u"),
            ("ses_g2", "Greeting", "/home/u"),
            ("ses_work", "Only in Work", "/home/u/Work"),
        ])
        con.commit()
        con.close()

    def lookup(self, title, directory="/home/u"):
        return m.opencode_session_by_title(title, directory, db=self.db)

    def test_exact_title(self):
        self.assertEqual(self.lookup("OC | Omarchi workspace and hibernation issues"), "ses_one")

    def test_title_cut_by_the_tui(self):
        self.assertEqual(self.lookup("OC | Omarchy upgrade and configuration rec\u2026"), "ses_long")

    def test_repeated_title_is_not_guessed(self):
        self.assertIsNone(self.lookup("OC | Greeting"))

    def test_session_of_another_folder_is_not_used(self):
        self.assertIsNone(self.lookup("OC | Only in Work"))
        self.assertEqual(self.lookup("OC | Only in Work", "/home/u/Work"), "ses_work")

    def test_untitled_and_foreign_titles(self):
        self.assertIsNone(self.lookup("OpenCode"))
        self.assertIsNone(self.lookup("cigano@host:~"))
        self.assertIsNone(m.opencode_session_by_title("OC | x", "/home/u",
                                                      db=os.path.join(self.tmp, "missing.db")))

    def test_resume_uses_the_title_and_falls_back_to_a_plain_shell(self):
        spec = m.AGENTS["opencode"]
        patches = {
            "children": lambda pid: [100] if pid == 1 else [],
            "match_agent": lambda pid: ("opencode", spec, ["--continue"], "opencode") if pid == 100 else None,
            "cwd_of": lambda pid: "/home/u",
            "opencode_db_path": lambda: self.db,
        }
        for name, fn in patches.items():
            self.addCleanup(setattr, m, name, getattr(m, name))
            setattr(m, name, fn)
        got = m.agent_resume(1, "OC | Omarchi workspace and hibernation issues")
        self.assertEqual(got["resume"], ["opencode", "--session", "ses_one"])
        # No id: never `--continue` (it attaches every terminal to one session).
        self.assertIsNone(m.agent_resume(1, "OC | Greeting"))
        self.assertIsNone(m.agent_resume(1, "OpenCode"))


if __name__ == "__main__":
    unittest.main()
