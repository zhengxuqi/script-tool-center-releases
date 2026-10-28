"""Validated local category tree. Incomplete caches never mean 'all categories'."""

import json
from datetime import datetime
from pathlib import Path

from coverage import ROOT


class CategoryCatalog:
    def __init__(self, path=None):
        self.path = Path(path) if path else ROOT / "data/category_catalog.json"
        self.parents = {}
        self.updated_at = None
        self.complete = False
        self.load_error = None
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.replace_parents(data["parents"], data.get("updated_at"),
                                     data.get("complete", False) and data.get("version", 1) >= 2)
            except (ValueError, KeyError, TypeError, OSError):
                self.load_error = "本地类目库无法读取，已使用内置个护类目；请刷新一级列表。"
        if not self.parents:
            seed = json.loads((ROOT / "data/categories_50002768.json").read_text(encoding="utf-8"))
            self.replace_parents([{"category_id": seed["parent_category_id"], "category_name": seed["parent_category_name"],
                                   "children": seed["categories"], "children_loaded": True,
                                   "updated_at": seed["read_date"]}], complete=False)

    @staticmethod
    def node(row, level, parent=None):
        identifier, name = str(row["category_id"]), str(row["category_name"]).strip()
        if not identifier.isascii() or not identifier.isdigit() or not name:
            raise ValueError("类目名称或 ID 无效，未更新类目库")
        result = {"category_id": identifier, "category_name": name, "level": level}
        if parent:
            result.update(parent_category_id=parent["category_id"], parent_category_name=parent["category_name"])
            if level == 3:
                result.update(root_category_id=parent["parent_category_id"],
                              root_category_name=parent["parent_category_name"])
        return result

    def replace_parents(self, rows, updated_at=None, complete=True):
        if not isinstance(rows, list) or not rows:
            raise ValueError("一级类目列表为空，保留原类目库")
        parents, all_ids = {}, set()
        for row in rows:
            parent = self.node(row, 1)
            identifier = parent["category_id"]
            if identifier in parents:
                raise ValueError("一级类目 ID 重复，未更新类目库")
            old = self.parents.get(identifier, {})
            loaded = row.get("children_loaded", old.get("children_loaded", False))
            children = row.get("children", old.get("children", []))
            normalized = []
            if identifier in all_ids:
                raise ValueError("类目 ID 重复，未更新类目库")
            all_ids.add(identifier)
            for item in children:
                second = self.node(item, 2, parent)
                grandchildren = []
                for grandchild in item.get("children", []):
                    third = self.node(grandchild, 3, second)
                    if str(grandchild.get("parent_category_id", second["category_id"])) != second["category_id"]:
                        raise ValueError("三级类目父级不匹配，未更新类目库")
                    if third["category_id"] in all_ids:
                        raise ValueError("类目 ID 重复，未更新类目库")
                    all_ids.add(third["category_id"])
                    grandchildren.append(third)
                if str(item.get("parent_category_id", identifier)) != identifier:
                    raise ValueError("二级类目父级不匹配，未更新类目库")
                if second["category_id"] in all_ids:
                    raise ValueError("类目 ID 重复，未更新类目库")
                all_ids.add(second["category_id"])
                second.update(children=grandchildren,
                              children_loaded=bool(item.get("children_loaded", False)))
                normalized.append(second)
            parent.update(children=normalized, children_loaded=bool(loaded),
                          updated_at=row.get("updated_at", old.get("updated_at")))
            parents[identifier] = parent
        self.parents = parents
        self.complete = bool(complete)
        self.updated_at = updated_at or self.updated_at

    def set_children(self, parent_id, rows, updated_at=None):
        parent = self.parents[str(parent_id)]
        if not isinstance(rows, list) or not rows:
            raise ValueError("未读到二级类目，保留原缓存，请检查页面加载或搜索条件")
        children = []
        for row in rows:
            if str(row.get("parent_category_id", parent_id)) != str(parent_id):
                raise ValueError("二级类目父级不匹配，未更新类目库")
            second = self.node(row, 2, parent)
            second.update(children=[self.node(item, 3, second) for item in row.get("children", [])],
                          children_loaded=bool(row.get("children_loaded", False)))
            children.append(second)
        if len({item["category_id"] for item in children}) != len(children):
            raise ValueError("二级类目 ID 重复，未更新类目库")
        parent.update(children=children, children_loaded=True,
                      updated_at=updated_at or datetime.now().astimezone().isoformat(timespec="seconds"))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 2, "complete": self.complete, "updated_at": self.updated_at,
                                    "parents": list(self.parents.values())}, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def resolve(self, ids, mode):
        if mode not in {"first", "second", "third"}:
            raise ValueError("未知的类目查询方式")
        if mode == "first":
            nodes = {p["category_id"]: self.node(p, 1) for p in self.parents.values()}
        elif mode == "second":
            nodes = {child["category_id"]: child for parent in self.parents.values() for child in parent["children"]}
        else:
            nodes = {child["category_id"]: child for parent in self.parents.values()
                     for second in parent["children"] for child in second["children"]}
        result = []
        for identifier in ids:
            identifier = str(identifier)
            if identifier not in nodes:
                raise ValueError(f"所选类目 {identifier} 已不在当前类目库中，请重新选择")
            if identifier not in {row["category_id"] for row in result}:
                result.append(dict(nodes[identifier]))
        return result
