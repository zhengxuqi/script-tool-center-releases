import json
from pathlib import Path
from tool_sdk import run, atomic_json


def health():
    import DrissionPage, requests, openpyxl, psutil
    import batch, category_catalog, category_reader
    import original_webapp, portal_service
    if not (Path(__file__).parent / 'web/index.html').is_file():
        raise ValueError('原达摩盘页面缺失。')


def plan(ctx):
    import coverage
    coverage.ROOT = ctx.data
    from category_catalog import CategoryCatalog
    from batch import periods_between, make_plan
    catalog = CategoryCatalog(ctx.data / 'category_catalog.json')
    lookup = {}
    for parent in catalog.parents.values():
        lookup[parent['category_id']] = parent
        for second in parent.get('children', []):
            lookup[second['category_id']] = second
            for third in second.get('children', []):
                lookup[third['category_id']] = third
    ids = [str(v) for v in ctx.params.get('category_ids', [])]
    if not ids or len(ids) != len(set(ids)) or any(v not in lookup for v in ids):
        raise ValueError('请选择有效且不重复的类目。')
    # Export headings are data, never executable spreadsheet expressions.
    categories = [{k: v for k, v in lookup[key].items() if k != 'children'} for key in ids]
    bands = ctx.params.get('price_ranges', [])
    periods = periods_between(ctx.params['start_date'], ctx.params['end_date'], ctx.params.get('monthly', True))
    settings = {'categories': categories, 'price_ranges': bands, 'periods': periods,
                'category_price_ranges': ctx.params.get('category_price_ranges', {})}
    jobs = make_plan(settings)
    if len(jobs) > 100000:
        raise ValueError('单次任务最多 100000 个条件，请缩小查询范围。')
    return settings, jobs


def main(ctx):
    import coverage
    coverage.ROOT = ctx.data
    from batch import make_plan, save_progress, load_progress, result_key, export_excel
    action = ctx.request['action']
    if action == 'preview':
        settings, jobs = plan(ctx)
        return {'total': len(jobs), 'categories': len(settings['categories']), 'periods': len(settings['periods'])}
    ctx.verify_browser()
    browser = ctx.request['browser']
    if action == 'browser':
        coverage.open_dmp_page(Path(browser['profile']), port=browser['port'])
        return {'message': '已打开达摩盘，请在浏览器中完成登录。'}
    if action == 'categories':
        from category_reader import CategoryReader
        from category_catalog import CategoryCatalog
        reader = CategoryReader(browser['profile'], browser['port'], log=ctx.log, stop=ctx.stop)
        value = reader.read()
        catalog = CategoryCatalog(ctx.data / 'category_catalog.json')
        catalog.replace_parents(value['parents'], value['updated_at'], complete=True)
        catalog.save()
        return {'message': '完整类目已同步。'}
    if action != 'collect':
        raise ValueError('不支持的工具操作。')
    results = []
    if ctx.request.get('resume_path'):
        saved = load_progress(Path(ctx.request['resume_path']))
        settings, results = saved['settings'], saved['results']
        jobs = make_plan(settings)
    else:
        settings, jobs = plan(ctx)
    atomic_json(ctx.data / 'settings.json', ctx.params)
    done = {result_key(r['conditions']) for r in results}
    pending = [job for job in jobs if result_key(job) not in done]
    checkpoint = ctx.output / 'progress.json'
    output = ctx.output / '达摩盘覆盖人数.xlsx'
    ctx.progress(len(results), len(jobs))
    save_progress(checkpoint, settings, results)
    def received(index, value):
        results.append(value)
        save_progress(checkpoint, settings, results)
        ctx.progress(len(results), len(jobs))
        ctx.log(f"已完成 {len(results)} / {len(jobs)} 个查询条件。")
    error = None
    try:
        if pending:
            coverage.browser_coverage(pending[0], Path(browser['profile']), port=browser['port'],
                                      query_configs=pending, on_result=received, stop=ctx.stop, interval=1, log=ctx.log)
    except Exception as exc:
        error = exc
    finally:
        save_progress(checkpoint, settings, results, '未完成' if error else '完成',
                      str(error) if isinstance(error, ValueError) else None)
        export_excel(checkpoint, output)
    if error:
        raise error
    return {'filename': output.name, 'completed': len(results), 'total': len(jobs)}


if __name__ == '__main__':
    run(main, health)
