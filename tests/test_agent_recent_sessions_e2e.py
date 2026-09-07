"""Unified recent-session source and filtering. Code version: v1.2.1-codex.1."""

import pytest
from playwright.sync_api import expect
from tests import test_sidebar_e2e as fixtures
from tests.agent_browser_fixtures import browser_profile_identity

disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


@pytest.mark.parametrize('width,theme', [(1008, 'light'), (733, 'light'), (390, 'dark')])
def test_unified_recent_sessions_filter_count_and_continue(disposable_browser, sidebar_server_url, width, theme):
    context = disposable_browser.new_context(viewport={'width': width, 'height': 1452}, color_scheme=theme)
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    project = 'https://chatgpt.com/g/g-p-demo/project'
    other_project = 'https://chatgpt.com/g/g-p-other/project'
    sessions = [dict(base['agent'], session_id=f's{i}', run_id=f'r{i}', running=False,
                     phase='finished', workspace_path='/tmp/demo', browser='edge', platform='chatgpt',
                     session_title=f'Local {i}', conversation_url=f'https://chatgpt.com/c/local-{i}',
                     project_url=project if i == 0 else other_project) for i in range(3)]
    catalog = {'profile_identity': browser_profile_identity(), 'recent_sessions': [
        {'url': sessions[0]['conversation_url'], 'title': 'Local zero'},
        {'url': 'https://chatgpt.com/c/remote-all', 'title': 'Remote all'}],
        'projects': [{'url': project, 'title': 'Demo project'}]}
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/agent/status', lambda route: route.fulfill(json={**base,
        'agent': {'session_id': 'new'}, 'sessions': sessions,
        'active_count': sum(item['running'] for item in sessions), 'can_start': True}))
    page.route('**/api/browser-session**', lambda route: route.fulfill(json={
        'profile_identity': browser_profile_identity(),
        'can_download': True, 'browser': 'edge', 'platform': 'chatgpt', 'agent_sources': catalog}))
    page.route('**/api/agent/sources**', lambda route: route.fulfill(json=catalog))
    page.route('**/api/agent/project-sessions**', lambda route: route.fulfill(json={
        'profile_identity': browser_profile_identity(),
        'sessions': [{'url': 'https://chatgpt.com/c/remote-project', 'title': 'Remote project'}]}))
    page.route('**/api/agent/chatgpt-session-history**', lambda route: route.fulfill(json={
        'profile_identity': browser_profile_identity(), 'history': []}))
    submitted = []

    def ask(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(json={**base, 'agent': dict(base['agent'], session_id='created', running=True, phase='running')})

    page.route('**/api/agent/ask', ask)
    try:
        page.goto(f'{sidebar_server_url}/agent/edge/chatgpt')
        if width < 900:
            page.locator('#sidebar_toggle').click()
        rail = page.locator('[data-agent-execution-sessions]')
        expect(rail.locator('summary')).to_have_text('Recent sessions')
        expect(page.locator('[data-agent-recent-session-field]')).to_have_count(0)
        expect(page.locator('[data-agent-project-session-field]')).to_have_count(0)
        expect(rail.locator('summary')).to_have_text('Recent sessions')
        source = page.locator('.agent-session-mode-combobox')
        expect(source.locator('[data-agent-combobox-selected-label]')).to_have_text('New session')
        source.locator('[data-agent-combobox-trigger]').click()
        options = source.locator('[role=option]')
        expect(options).to_have_text(['New session', 'Projects'])
        for option in options.all():
            expect(option).to_have_css('height', '36px')
        source.locator('[data-agent-combobox-option=new]').click()
        expect(rail.locator('.agent-execution-session')).to_have_count(4)
        geometry = page.evaluate("""() => {
            const label = document.querySelector('[data-agent-session-platform-label]');
            const summary = document.querySelector('[data-agent-execution-sessions] summary');
            const trigger = document.querySelector('.agent-session-mode-combobox [data-agent-combobox-trigger]');
            const row = document.querySelector('.agent-execution-session');
            const font = el => { const s=getComputedStyle(el); return [s.fontFamily,s.fontSize,s.fontWeight,s.lineHeight]; };
            const bounds = el => { const r=el.getBoundingClientRect(); return [r.left,r.right]; };
            return {label:font(label),summary:font(summary),trigger:bounds(trigger),row:bounds(row)};
        }""")
        assert geometry['label'] == geometry['summary']
        assert all(abs(a-b) < 1 for a,b in zip(geometry['trigger'],geometry['row']))

        badge = rail.locator('[data-agent-session-capacity]')
        expect(badge).to_be_hidden()
        for active in (1, 3, 0):
            for index, item in enumerate(sessions):
                item['running'] = index < active
            if active:
                expect(badge).to_have_text(str(active), timeout=6000)
                expect(badge).to_be_visible()
                bounds = badge.bounding_box()
                assert abs(bounds['height'] - bounds['width']) < 1
            else:
                expect(badge).to_be_hidden(timeout=6000)
        source.locator('[data-agent-combobox-trigger]').click()
        source.locator('[data-agent-combobox-option=project]').click()
        projects = page.locator('[data-agent-session-list=projects]')
        projects.locator('[data-agent-combobox-trigger]').click()
        projects.get_by_role('option', name='Demo project', exact=True).click()
        expect(rail.locator('.agent-execution-session')).to_have_count(2)
        expect(rail).to_contain_text('Remote project')
        expect(rail).not_to_contain_text('Local 1')
        rail.get_by_role('button', name='Remote project', exact=True).click()
        expect(page.locator('input[name=conversation_url]')).to_have_value('https://chatgpt.com/c/remote-project')
        expect(page.locator('input[name=session_mode]')).to_have_value('project_session')
        expect(page.locator('input[name=project_url]')).to_have_value(project)
        page.reload(wait_until='domcontentloaded')
        if width < 900 and not rail.is_visible():
            page.locator('#sidebar_toggle').click()
        expect(rail.locator('.agent-execution-session')).to_have_count(2)
        expect(rail.get_by_role('button', name='Remote project', exact=True)).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('input[name=conversation_url]')).to_have_value('https://chatgpt.com/c/remote-project')
        source.locator('[data-agent-combobox-trigger]').click()
        source.locator('[data-agent-combobox-option=new]').click()
        expect(rail.locator('.agent-execution-session')).to_have_count(4)
        rail.get_by_role('button', name='Remote all', exact=True).click()
        expect(page.locator('input[name=conversation_url]')).to_have_value('https://chatgpt.com/c/remote-all')
        if width < 900:
            page.locator('#sidebar_toggle').click()
        page.get_by_placeholder('Do anything', exact=True).fill('Continue this conversation.')
        page.get_by_role('button', name='Ask ChatGPT Web', exact=True).click()
        expect(page.get_by_role('button', name='Stop Agent task', exact=True)).to_be_visible()
        assert submitted[0]['conversation_url'] == 'https://chatgpt.com/c/remote-all'
        assert submitted[0]['session_mode'] == 'recent'
        assert not errors
    finally:
        context.close()
