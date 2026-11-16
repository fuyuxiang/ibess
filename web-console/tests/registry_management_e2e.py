"""基础管理真实浏览器验收；所有写操作通过页面，只读 API 核对事实。

部署完成后运行：python tests/registry_management_e2e.py --base-url http://localhost:3000
准备检查：python tests/registry_management_e2e.py --self-check（不访问浏览器/服务）
不生成事件、不录入照片。仅删除本次 UUID 和编号均匹配的临时配置。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import traceback
from urllib.parse import urlparse
import uuid

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parent
ENTITIES = {"projects": ("项目", "project_id"), "persons": ("人员", "person_id"),
            "rooms": ("评标室", "room_id"), "seats": ("工位", "seat_id"),
            "participants": ("关联人员", "membership_id")}
WIDTHS = (1366, 1600, 1920)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def assert_associations(data, ids):
    rows = {entity: next((row for row in data[entity] if row[key] == ids[entity]), None)
            for entity, (_, key) in ENTITIES.items()}
    require(all(rows.values()), "刷新后五类临时记录必须全部存在")
    require(rows["projects"]["status"] == "running", "项目须为进行中")
    require(rows["rooms"]["mode"] == "centralized", "评标室须为集中评标")
    require(rows["persons"]["unit"] == "浏览器回归演示单位", "单位须持久化")
    require(rows["persons"]["role"] == "supervisor", "人员默认角色须持久化")
    require(rows["participants"]["role"] == "expert", "项目内角色须独立保存")
    require(rows["rooms"]["project_id"] == ids["projects"], "评标室项目 UUID 不符")
    require(rows["seats"]["room_id"] == ids["rooms"], "工位评标室 UUID 不符")
    require(rows["seats"]["project_id"] == ids["projects"], "工位派生项目 UUID 不符")
    require(rows["seats"]["person_id"] == ids["persons"], "工位人员 UUID 不符")
    require(rows["participants"]["project_id"] == ids["projects"] and
            rows["participants"]["person_id"] == ids["persons"], "成员 UUID 关联不符")
    return rows


def overflow_metrics(page):
    return page.evaluate("""() => {
      const root = document.querySelector('.registry-page');
      const box = root.getBoundingClientRect();
      return {viewport: innerWidth, document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth, registryLeft: box.left, registryRight: box.right,
        tableScrollers: [...root.querySelectorAll('.ant-table-content,.ant-table-body')]
          .map(e => ({clientWidth:e.clientWidth, scrollWidth:e.scrollWidth}))};
    }""")


class RegistryAcceptance:
    def __init__(self, page, base, report):
        self.page, self.base, self.report = page, base, report
        self.root = page.locator(".registry-page")
        self.ids = report["created_ids"]
        self.marker = report["marker"]
        self.codes = {entity: f"浏览器回归演示-{entity}-{report['run_id']}"
                      for entity in ENTITIES if entity != "participants"}
        self.names = {entity: f"浏览器回归演示-{label}-{report['run_id']}"
                      for entity, (label, _) in ENTITIES.items()}

    def snapshot(self):
        response = self.page.request.get(self.base + "/api/registry")
        require(response.ok, f"只读配置核对失败 HTTP {response.status}: {response.text()}")
        data = response.json()
        require(all(isinstance(data.get(entity), list) for entity in ENTITIES), "registry 返回结构不符")
        return data

    def open(self):
        with self.page.expect_response(lambda r: urlparse(r.url).path == "/api/registry" and
                                      r.request.method == "GET") as response:
            self.page.goto(self.base + "/registry", wait_until="domcontentloaded")
        require(response.value.ok, f"部署未就绪：registry HTTP {response.value.status}")
        # Shell 定时刷新全局数据，不能用 networkidle 判断此 SPA 是否就绪。
        # 以本页 registry GET 与标题/操作按钮的可见、可用状态为准。
        expect(self.root.get_by_role("heading", name=re.compile(r"基础管理$"))).to_be_visible()
        expect(self.root.get_by_role("button", name="刷新配置")).to_be_enabled()

    @contextmanager
    def case(self, number, name):
        record = {"id": f"REG-{number:02d}", "name": f"浏览器回归演示：{name}", "status": "RUNNING"}
        self.report["cases"].append(record)
        try:
            yield record
            record["status"] = "PASS"
            print("PASS", record["id"], record["name"], flush=True)
        except Exception as error:
            record.update(status="FAIL", error=str(error))
            self.capture(f"failure-{number:02d}", record)
            raise

    def capture(self, name, record=None):
        target = ROOT / f"registry-management-{self.report['run_id']}-{name}.png"
        try:
            # Tabs 指示条有过渡动画，截图应等到选中态稳定。
            self.page.wait_for_timeout(350)
            self.page.screenshot(path=str(target), full_page=True)
            self.report["screenshots"].append(str(target))
            if record is not None:
                record["screenshot"] = str(target)
        except Exception as error:
            self.report.setdefault("screenshot_errors", []).append(str(error))

    def mutation(self, method, entity, click, expected=(200, 201), entity_id=None):
        path = f"/api/registry/{entity}" + (f"/{entity_id}" if entity_id else "")
        with self.page.expect_response(lambda r: urlparse(r.url).path == path and
                                      r.request.method == method) as pending:
            click()
        response = pending.value
        self.report["ui_mutations"].append({"method": method, "entity": entity,
            "path": path, "status": response.status, "via": "UI", "body": response.json()})
        require(response.status in expected, f"{method} {entity}: HTTP {response.status} {response.text()}")
        expect(self.root.get_by_role("button", name="刷新配置")).to_be_enabled()
        return response.json()

    def tab(self, entity, keyword=None):
        self.root.get_by_role("tab", name=ENTITIES[entity][0], exact=True).click()
        expect(self.root.get_by_role("tab", name=ENTITIES[entity][0], exact=True)).to_have_attribute("aria-selected", "true")
        expect(self.root.get_by_role("tabpanel", name=ENTITIES[entity][0], exact=True)).to_be_visible()
        self.root.get_by_role("textbox", name="搜索基础配置").fill(keyword if keyword is not None else self.codes[entity])
        expect(self.root.get_by_role("button", name="刷新配置")).to_be_enabled()

    def row(self, entity):
        _, key = ENTITIES[entity]
        row = self.root.locator(f'tr.ant-table-row[data-row-key="{self.ids[entity]}"]:visible')
        expect(row).to_have_count(1)
        return row

    def dialog(self):
        dialog = self.page.get_by_role("dialog").filter(visible=True)
        expect(dialog).to_have_count(1)
        return dialog

    def choose(self, scope, label, option):
        field = scope.locator(".ant-form-item").filter(
            has=scope.page.locator("label").get_by_text(label, exact=True))
        expect(field).to_have_count(1)
        field.locator(".ant-select-selector").click()
        popup = self.page.locator(".ant-select-dropdown:visible")
        expect(popup).to_have_count(1)
        choice = popup.locator(".ant-select-item-option").filter(has_text=option)
        expect(choice).to_have_count(1)
        choice.click()

    def editor(self, entity, edit=False):
        self.tab(entity)
        if edit:
            self.row(entity).get_by_role("button", name=re.compile(r"编辑$" )).click()
        else:
            self.root.get_by_role("button", name=re.compile(f"新增{ENTITIES[entity][0]}$")).click()
        dialog = self.dialog()
        expect(dialog.get_by_role("button", name=re.compile(r"保\s*存$"))).to_be_enabled()
        return dialog

    def save(self, entity, dialog, edit=False):
        data = self.mutation("PUT" if edit else "POST", entity,
            lambda: dialog.get_by_role("button", name=re.compile(r"保\s*存$")).click(),
            entity_id=self.ids.get(entity) if edit else None)
        if not edit:
            self.ids[entity] = data[ENTITIES[entity][1]]
        expect(dialog).to_have_count(0)
        expect(self.row(entity)).to_contain_text(self.codes[entity])
        return data

    def members(self):
        self.tab("projects")
        self.row("projects").get_by_role("button", name="关联人员", exact=True).click()
        return self.dialog()

    def close_dialog(self):
        self.dialog().get_by_role("button", name="Close", exact=True).click()
        expect(self.page.get_by_role("dialog")).to_have_count(0)

    def confirmation(self, scope, trigger, confirmation):
        scope.get_by_role("button", name=trigger, exact=True).click()
        popup = self.page.locator(".ant-popconfirm:visible")
        expect(popup).to_have_count(1)
        # Ant Design 自动给两个汉字的按钮插入空格，如“删 除”。
        pattern = r"\s*".join(re.escape(character) for character in confirmation)
        return lambda: popup.get_by_role("button", name=re.compile(pattern + r"$")).click()

    def protected(self, entity):
        self.tab(entity)
        click = self.confirmation(self.row(entity), "删除", "删除")
        data = self.mutation("DELETE", entity, click, expected=(409,), entity_id=self.ids[entity])
        alert = self.root.locator(".ant-alert-error")
        expect(alert).to_be_visible()
        require(str(data.get("detail", "")).strip(), "删除保护应返回具体错误")
        expect(alert).to_contain_text(data["detail"])
        expect(self.row(entity)).to_be_visible()
        require(any(row[ENTITIES[entity][1]] == self.ids[entity] for row in self.snapshot()[entity]),
                "删除失败不得丢失记录")

    def delete(self, entity):
        # GET 再校验 UUID、编号与运行标记，防止误删其他人的数据。
        rows = self.snapshot()[entity]
        key = ENTITIES[entity][1]
        record = next((row for row in rows if row[key] == self.ids.get(entity)), None)
        if record is None:
            return
        require(record["code"] == self.codes[entity], "临时记录编号不符，拒绝删除")
        self.tab(entity)
        click = self.confirmation(self.row(entity), "删除", "删除")
        self.mutation("DELETE", entity, click, expected=(200,), entity_id=self.ids[entity])
        expect(self.root.locator(f'tr[data-row-key="{self.ids[entity]}"]:visible')).to_have_count(0)

    def remove_member(self, protected=False):
        dialog = self.members()
        row = dialog.locator(f'tr.ant-table-row[data-row-key="{self.ids["participants"]}"]')
        expect(row).to_contain_text(self.names["persons"])
        click = self.confirmation(row, "移除", "解除关联")
        data = self.mutation("DELETE", "participants", click,
            expected=(409,) if protected else (200,), entity_id=self.ids["participants"])
        if protected:
            expect(dialog.locator(".ant-alert-error")).to_contain_text(data["detail"])
            expect(row).to_have_count(1)
        else:
            expect(row).to_have_count(0)
        self.close_dialog()

    def run(self):
        with self.case(1, "部署入口及真实表单非空校验"):
            self.open()
            initial = self.snapshot()
            seed_projects = [row["project_id"] for row in initial["projects"] if row["code"] == "DEMO"]
            self.report["initial_seed_ids"] = {"projects": seed_projects,
                "rooms": [row["room_id"] for row in initial["rooms"] if row["project_id"] in seed_projects]}
            dialog = self.editor("projects")
            before = len(self.report["ui_mutations"])
            dialog.get_by_role("button", name=re.compile(r"保\s*存$")).click()
            expect(dialog.get_by_text("请输入非空内容", exact=True).first).to_be_visible()
            require(len(self.report["ui_mutations"]) == before, "空表单不得计为创建成功")
            dialog.get_by_role("button", name=re.compile(r"取\s*消$")).click()

        with self.case(2, "UI 新增进行中项目"):
            dialog = self.editor("projects")
            dialog.get_by_label(re.compile(r"项目编号$")).fill(self.codes["projects"])
            dialog.get_by_label(re.compile(r"名称$")).fill(self.names["projects"])
            dialog.get_by_label(re.compile(r"业主 / 负责人$")).fill("浏览器回归演示负责人")
            self.choose(dialog, "项目状态", "进行中")
            require(self.save("projects", dialog)["status"] == "running", "项目状态保存错误")

        with self.case(3, "UI 新增人员、单位与监督角色，无人脸照片"):
            dialog = self.editor("persons")
            dialog.get_by_label(re.compile(r"人员编号$")).fill(self.codes["persons"])
            dialog.get_by_label(re.compile(r"姓名$")).fill(self.names["persons"])
            dialog.get_by_label(re.compile(r"单位$")).fill("浏览器回归演示单位")
            self.choose(dialog, "默认角色", "监督人员")
            require(dialog.locator('input[type="file"]').count() == 0, "人员表单不应要求照片")
            require(self.save("persons", dialog)["role"] == "supervisor", "人员角色保存错误")

        with self.case(4, "UI 新增集中评标室并 UUID 关联项目"):
            dialog = self.editor("rooms")
            dialog.get_by_label(re.compile(r"评标室编号$")).fill(self.codes["rooms"])
            dialog.get_by_label(re.compile(r"名称$")).fill(self.names["rooms"])
            self.choose(dialog, "所属项目", self.codes["projects"])
            dialog.get_by_label(re.compile(r"位置$")).fill("浏览器回归演示临时位置")
            self.choose(dialog, "评标模式", "集中评标")
            self.save("rooms", dialog)

        with self.case(5, "UI 关联项目参加人员与独立项目角色"):
            dialog = self.members()
            self.choose(dialog, "参加人员", self.codes["persons"])
            self.choose(dialog, "项目内角色", "评标专家")
            member = self.mutation("POST", "participants",
                lambda: dialog.get_by_role("button", name="添加关联", exact=True).click())
            self.ids["participants"] = member["membership_id"]
            expect(dialog.locator(f'tr[data-row-key="{member["membership_id"]}"]')).to_contain_text("评标专家")
            self.close_dialog()

        with self.case(6, "UI 新增工位并分配同项目已关联人员"):
            dialog = self.editor("seats")
            dialog.get_by_label(re.compile(r"工位编号$")).fill(self.codes["seats"])
            self.choose(dialog, "所属评标室", self.codes["rooms"])
            self.choose(dialog, "分配人员（可选）", self.codes["persons"])
            self.save("seats", dialog)
            assert_associations(self.snapshot(), self.ids)

        with self.case(7, "整页刷新保留五类配置及关联"):
            self.open()
            assert_associations(self.snapshot(), self.ids)
            for entity in ("projects", "persons", "rooms", "seats"):
                self.tab(entity)
                expect(self.row(entity)).to_contain_text(self.codes[entity])
            self.capture("assigned-persisted")

        with self.case(8, "UI 编辑人员与评标室名称并刷新核对"):
            for entity, label in (("persons", "姓名"), ("rooms", "名称")):
                dialog = self.editor(entity, edit=True)
                self.names[entity] += "-已编辑"
                dialog.get_by_label(re.compile(re.escape(label) + r"$")).fill(self.names[entity])
                self.save(entity, dialog, edit=True)
            self.open()
            rows = assert_associations(self.snapshot(), self.ids)
            for entity in ("persons", "rooms"):
                require(rows[entity]["name"] == self.names[entity], "编辑名称未持久化")
            self.tab("seats")
            expect(self.row("seats")).to_contain_text(self.names["persons"])
            expect(self.row("seats")).to_contain_text(self.names["rooms"])

        with self.case(9, "搜索编号、编辑后名称、项目、单位、角色及零结果"):
            for entity, keyword in (("projects", self.codes["projects"]),
                ("persons", self.names["persons"]), ("persons", "浏览器回归演示单位"),
                ("persons", "监督人员"), ("rooms", self.names["rooms"]),
                ("rooms", self.codes["projects"]), ("seats", self.names["persons"])):
                self.tab(entity, keyword)
                expect(self.row(entity)).to_be_visible()
            self.tab("rooms", "不存在-" + uuid.uuid4().hex)
            expect(self.root.get_by_text("没有匹配的配置", exact=True).filter(visible=True)).to_be_visible()
            expect(self.root.locator("tr.ant-table-row:visible")).to_have_count(0)
            self.tab("rooms")
            expect(self.row("rooms")).to_be_visible()

        with self.case(10, "存在子项或工位分配时拒绝删除项目、室、人员及成员"):
            for entity in ("projects", "rooms", "persons"):
                self.protected(entity)
            self.remove_member(protected=True)
            assert_associations(self.snapshot(), self.ids)

        with self.case(11, "1366 / 1600 / 1920 四标签无页面横向溢出、无 JS 异常") as record:
            alert_close = self.root.locator(".ant-alert-close-icon:visible")
            if alert_close.count():
                alert_close.click()
            for width in WIDTHS:
                self.page.set_viewport_size({"width": width, "height": 1000})
                for entity in ("projects", "rooms", "seats", "persons"):
                    self.tab(entity)
                    expect(self.row(entity)).to_be_visible()
                    metrics = overflow_metrics(self.page)
                    self.report["layout_checks"].append({"width": width, "tab": entity, **metrics})
                    self.capture(f"{width}-{entity}")
                    require(metrics["document"] <= width + 2 and metrics["body"] <= width + 2,
                            f"{width} {entity} 页面横向溢出：{metrics}")
                    require(metrics["registryLeft"] >= -2 and metrics["registryRight"] <= width + 2,
                            f"{width} {entity} 管理区域超出视口：{metrics}")
            require(not self.report["page_errors"], f"JS 异常：{self.report['page_errors']}")
            record["internal_table_scroll_allowed"] = True

        with self.case(12, "UI 取消工位分配并刷新保留"):
            self.page.set_viewport_size({"width": 1600, "height": 1000})
            self.tab("seats")
            click = self.confirmation(self.row("seats"), "取消分配", "取消分配")
            self.mutation("PUT", "seats", click, entity_id=self.ids["seats"])
            self.open()
            self.tab("seats")
            expect(self.row("seats")).to_contain_text("未分配")
            require(next(row for row in self.snapshot()["seats"] if row["seat_id"] == self.ids["seats"])["person_id"] is None,
                    "取消分配未持久化")

        with self.case(13, "UI 解除项目成员并刷新保留"):
            self.remove_member()
            self.open()
            require(not any(row["membership_id"] == self.ids["participants"] for row in self.snapshot()["participants"]),
                    "解除关联未持久化")

        with self.case(14, "UI 顺序删除工位、室、人员、项目且不残留临时记录"):
            for entity in ("seats", "rooms", "persons", "projects"):
                self.delete(entity)
            self.open()
            remaining = self.snapshot()
            for entity, (_, key) in ENTITIES.items():
                require(not any(row[key] == self.ids[entity] for row in remaining[entity]), "临时记录清理不完整")
            for entity, initial_ids in self.report["initial_seed_ids"].items():
                actual_ids = {row[ENTITIES[entity][1]] for row in remaining[entity]}
                require(set(initial_ids) <= actual_ids, "初始演示配置必须保留")
            self.report["seed_configuration_preserved"] = True
            posted = {entry["entity"] for entry in self.report["ui_mutations"]
                      if entry["method"] == "POST" and entry["status"] in (200, 201)}
            require(posted == set(ENTITIES), f"五类创建应全部经 UI 完成：{posted}")
            require(not self.report["page_errors"], "浏览器存在 JS 异常")
            self.report["cleanup"] = "所有本次临时记录已通过 UI 顺序删除"

    def cleanup_previous(self):
        """失败重跑前仅清理报告内、编号核对一致的本次记录，仍全部通过 UI。"""
        self.open()
        data = self.snapshot()
        for entity in ("persons", "rooms"):
            record = next((row for row in data[entity] if row[ENTITIES[entity][1]] == self.ids.get(entity)), None)
            if record:
                require(record["code"] == self.codes[entity], "记录编号不符，拒绝清理")
                self.names[entity] = record["name"]
        seat = next((row for row in data["seats"] if row["seat_id"] == self.ids.get("seats")), None)
        if seat and seat["person_id"]:
            require(seat["code"] == self.codes["seats"], "工位编号不符，拒绝取消分配")
            self.tab("seats")
            click = self.confirmation(self.row("seats"), "取消分配", "取消分配")
            self.mutation("PUT", "seats", click, entity_id=self.ids["seats"])
        member = next((row for row in self.snapshot()["participants"] if row["membership_id"] == self.ids.get("participants")), None)
        if member:
            require(member["project_id"] == self.ids.get("projects") and member["person_id"] == self.ids.get("persons"), "成员关系不符，拒绝解除")
            self.remove_member()
        for entity in ("seats", "rooms", "persons", "projects"):
            self.delete(entity)
        data = self.snapshot()
        for entity, (_, key) in ENTITIES.items():
            require(not any(row[key] == self.ids.get(entity) for row in data[entity]), "临时记录清理不完整")
        self.report["cleanup"] = "失败运行的临时记录已全部通过 UI 清理"


def self_check():
    """离线核对脚本断言能捕获错误，绝不访问尚未部署的 API。"""
    ids = {entity: entity + "-uuid" for entity in ENTITIES}
    data = {
        "projects": [{"project_id": ids["projects"], "status": "running"}],
        "persons": [{"person_id": ids["persons"], "unit": "浏览器回归演示单位", "role": "supervisor"}],
        "rooms": [{"room_id": ids["rooms"], "project_id": ids["projects"], "mode": "centralized"}],
        "seats": [{"seat_id": ids["seats"], "project_id": ids["projects"], "room_id": ids["rooms"], "person_id": ids["persons"]}],
        "participants": [{"membership_id": ids["participants"], "project_id": ids["projects"], "person_id": ids["persons"], "role": "expert"}],
    }
    assert_associations(data, ids)
    caught = []
    for entity, field, invalid in (("projects", "status", "draft"), ("rooms", "project_id", "wrong"),
        ("rooms", "mode", "remote"), ("persons", "unit", ""), ("persons", "role", "expert"),
        ("participants", "role", "supervisor"), ("seats", "person_id", None)):
        broken = json.loads(json.dumps(data))
        broken[entity][0][field] = invalid
        try:
            assert_associations(broken, ids)
        except AssertionError:
            caught.append(f"{entity}.{field}")
        else:
            raise AssertionError(f"red 检查未捕获 {entity}.{field}")
    report = {"status": "PREPARED", "network_access": False, "browser_launched": False,
        "live_acceptance_executed": False, "positive_fixture": "PASS", "red_checks_caught": caught,
        "planned_widths": WIDTHS, "ui_create_entities": list(ENTITIES),
        "awaiting": "用户通知部署完成后执行真实浏览器验收"}
    (ROOT / "registry-management-preparation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:3000")
    parser.add_argument("--self-check", action="store_true", help="离线检查，不打开页面、不访问 API")
    parser.add_argument("--cleanup-result", type=Path, help="仅通过 UI 清理指定失败报告中 UUID、编号一致的临时记录")
    args = parser.parse_args()
    if args.self_check:
        return self_check()
    base = args.base_url.rstrip("/")
    require(urlparse(base).scheme in ("http", "https"), "base-url 须为 HTTP(S) 地址")
    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    report = {"run_id": run_id, "marker": "浏览器回归演示", "base_url": base,
        "started_at": datetime.now(timezone.utc).isoformat(), "status": "RUNNING",
        "cases": [], "created_ids": {}, "ui_mutations": [], "layout_checks": [],
        "page_errors": [], "console_errors": [], "screenshots": [],
        "scope": "仅基础管理 UI；不测试事件 integration 或后端单元测试，不生成事件，不上传照片"}
    if args.cleanup_result:
        previous = json.loads(args.cleanup_result.read_text(encoding="utf-8"))
        require(previous.get("marker") == "浏览器回归演示", "只允许清理本脚本生成的回归报告")
        require(previous.get("base_url") == base, "报告环境不符，拒绝清理")
        report.update(run_id=previous["run_id"], created_ids=previous["created_ids"], cleanup_source=str(args.cleanup_result))
    result_path = ROOT / f"registry-management-{run_id}-result.json"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="zh-CN")
        context.set_default_timeout(15000)
        page = context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        page.on("console", lambda message: report["console_errors"].append(message.text) if message.type == "error" else None)
        acceptance = RegistryAcceptance(page, base, report)
        try:
            if args.cleanup_result:
                acceptance.cleanup_previous()
            else:
                acceptance.run()
            report["status"] = "PASS"
        except Exception as error:
            report.update(status="FAIL", error=str(error), traceback=traceback.format_exc())
            report["cleanup"] = "失败后保留已创建记录供排查；仅属于本次 created_ids，无事件引用"
            acceptance.capture("failure-final")
            print("FAIL", str(error), flush=True)
        finally:
            expected_409_count = sum(entry["status"] == 409 for entry in report["ui_mutations"])
            conflict_messages = [message for message in report["console_errors"]
                                 if "409 (Conflict)" in message]
            report["expected_conflict_console_count"] = min(expected_409_count, len(conflict_messages))
            report["unexpected_console_errors"] = [message for message in report["console_errors"]
                if "409 (Conflict)" not in message] + conflict_messages[expected_409_count:]
            if report["status"] == "PASS" and report["unexpected_console_errors"]:
                report.update(status="FAIL", error="发现非预期浏览器控制台错误")
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("RESULT", result_path, flush=True)
            context.close()
            browser.close()
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
