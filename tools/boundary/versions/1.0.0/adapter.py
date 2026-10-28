import copy
from tool_sdk import run, atomic_json


def health():
    import DrissionPage, requests, openpyxl, psutil
    from collector import BIClient
    from legacy_webapp import LocalApplication


def main(ctx):
    from collector import BIClient, group_category_tree
    from legacy_webapp import LocalApplication
    app = LocalApplication(root=ctx.data)
    params = ctx.params
    action = ctx.request['action']
    ctx.verify_browser()
    client = BIClient(**ctx.request['browser'], log=ctx.log, stop=ctx.stop).connect()
    if action == 'browser':
        return {'message': '浏览器已打开并取得登录状态。'}
    if action == 'categories':
        parent = str(params.get('parent', ''))
        if parent and not any(str(r['id']) == parent for r in app.catalog.get('roots', [])):
            raise ValueError('请先同步并选择一级类目。')
        rows = client.categories(parent or None)
        if not rows:
            raise ValueError('接口未返回可用类目，已保留原缓存。')
        catalog = copy.deepcopy(app.catalog)
        if parent:
            catalog.setdefault('children', {}).update(group_category_tree(rows, parent))
        else:
            catalog = {'roots': rows, 'children': {}}
        atomic_json(app.catalog_path, catalog)
        return {'message': '类目已同步。'}
    selection = app.selection(params)
    if action == 'time':
        limits = client.limits(selection.category_id)
        atomic_json(ctx.data / 'limits.json', {'category_id': selection.category_id, 'limits': limits})
        return {'limits': limits}
    if action == 'collect':
        app.save_settings({**params, **ctx.request['browser']})
        path = client.collect(selection, ctx.output)
        return {'filename': path.name}
    raise ValueError('不支持的工具操作。')


if __name__ == '__main__':
    run(main, health)
