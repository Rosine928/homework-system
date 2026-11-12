// Run against an isolated database; use installed Microsoft Edge, no browser download.
const { chromium } = require('../artifacts/browser-tools/node_modules/playwright');
const { spawn, spawnSync } = require('node:child_process');
const { mkdirSync, writeFileSync } = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
const target = path.join(root, 'artifacts', `acceptance-${new Date().toISOString().replace(/[:.]/g, '-')}`);
mkdirSync(target, { recursive: true });
const fixture = spawnSync(python, [path.join(__dirname, 'browser_fixture.py'), target], { cwd: root, encoding: 'utf8', windowsHide: true });
if (fixture.status !== 0) throw new Error(fixture.stderr || fixture.stdout);
const ids = JSON.parse(fixture.stdout.trim());
const port = 15053;
const base = `http://127.0.0.1:${port}`;
const server = spawn(python, ['app.py'], {
    cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PORT: String(port), HOMEWORK_DATABASE_URI: `sqlite:///${path.join(target, 'demo.db').replace(/\\/g, '/')}`, HOMEWORK_UPLOAD_FOLDER: path.join(target, 'uploads') },
});
let serverLog = '';
server.stdout.on('data', data => { serverLog += data; });
server.stderr.on('data', data => { serverLog += data; });
const issues = [];
const screenshots = [];
const checkedPages = [];
let browser;

async function submit(form, name) {
    await Promise.all([form.page().waitForNavigation(), form.getByRole('button', { name, exact: true }).click()]);
}
async function login(page, username) {
    await page.goto(`${base}/login`);
    await page.getByLabel('用户名', { exact: true }).fill(username);
    await page.getByLabel('密码', { exact: true }).fill('DemoPass123');
    await submit(page.locator('form[action="/login"]'), '登录');
    assert(page.url().endsWith('/dashboard'));
}
async function shot(page, filename) {
    await page.screenshot({ path: path.join(target, filename), fullPage: true });
    screenshots.push(filename);
}
async function assertLayout(page, label) {
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
    assert(!overflow, `Page overflow: ${label}`);
    checkedPages.push(label);
}
async function openSummary(page, text) {
    const summary = page.locator('summary').filter({ hasText: text }).first();
    await summary.evaluate(node => { node.parentElement.open = true; });
    return summary.locator('..');
}
async function main() {
    for (let attempt = 0; attempt < 60; attempt++) {
        try { if ((await fetch(`${base}/login`)).ok) break; } catch {}
        if (server.exitCode !== null) throw new Error(serverLog);
        if (attempt === 59) throw new Error('Demo server did not start');
        await new Promise(resolve => setTimeout(resolve, 250));
    }
    browser = await chromium.launch({ executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', headless: true });
    const teacherContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const studentContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    for (const context of [teacherContext, studentContext]) {
        context.on('page', page => {
            page.on('pageerror', error => issues.push(error.message));
            page.on('response', response => { if (response.status() >= 500) issues.push(`${response.status()} ${response.url()}`); });
        });
    }
    const teacher = await teacherContext.newPage();
    const student = await studentContext.newPage();
    await teacher.goto(`${base}/register`);
    await teacher.locator('#account-role').selectOption('TEACHER');
    assert(await teacher.locator('.student-field').isHidden());
    await teacher.locator('#account-role').selectOption('STUDENT');
    assert(await teacher.locator('#student-number').isVisible());
    await login(teacher, 'demo-teacher');
    await teacher.goto(`${base}/assignment/list`);
    await assertLayout(teacher, 'teacher-assignment-desktop');
    const create = (await openSummary(teacher, '发布新作业')).locator('form');
    await create.locator('[name="course_id"]').selectOption(String(ids.course_id));
    await create.locator('[name="class_id"]').selectOption(String(ids.class_id));
    await create.locator('[name="title"]').fill('课程作业管理系统 · 最终实验');
    await create.locator('[name="description"]').fill('实现作业提交、批改反馈和统计导出，并提交实验报告。');
    await create.locator('[name="due_at"]').fill('2030-12-01T18:00');
    await submit(create, '发布作业');
    assert(teacher.url().includes('/assignment/detail/'));
    const assignmentURL = teacher.url();
    const assignmentId = Number(assignmentURL.split('/').pop());
    const edit = (await openSummary(teacher, '作业管理')).locator('form').first();
    await edit.locator('[name="description"]').fill('实现作业提交、批改反馈和统计导出，并提交完整实验报告及截图。');
    await submit(edit, '保存作业信息');
    await login(student, 'demo-student');
    await student.goto(`${base}/notifications`);
    assert(await student.getByText('新作业发布', { exact: true }).count());
    await shot(student, '01-notifications.png');
    await student.goto(`${base}/profile`);
    await student.locator('#real-name').fill('林同学');
    await submit(student.locator('main form'), '保存资料');
    await student.goto(assignmentURL);
    const longName = '实验报告_'.repeat(12) + '.txt';
    await student.locator('input[type="file"]').setInputFiles({ name: longName, mimeType: 'text/plain', buffer: Buffer.from('First revision') });
    await submit(student.locator('form[enctype]'), '上传提交');
    await student.locator('input[type="file"]').setInputFiles({ name: '实验报告_第二版.txt', mimeType: 'text/plain', buffer: Buffer.from('Second revision') });
    await submit(student.locator('form[enctype]'), '上传新版本');
    await openSummary(student, '历史版本');
    await assertLayout(student, 'student-long-filename-desktop');
    await shot(student, '02-submission-versions.png');
    await teacher.goto(assignmentURL);
    const grade = teacher.locator('form.grade');
    assert.equal(await grade.count(), 1);
    await grade.locator('[name="score"]').fill('80');
    await grade.locator('[name="comment"]').fill('基本完成，请补充测试说明。');
    await submit(grade, '保存评分');
    await teacher.locator('form.grade [name="score"]').fill('85');
    await teacher.locator('form.grade [name="comment"]').fill('复核后修正分数，主要流程完整。');
    await submit(teacher.locator('form.grade'), '保存评分');
    await openSummary(teacher, '批改与期限调整记录');
    await shot(teacher, '03-grading-history.png');
    const redo = (await openSummary(teacher, '退回重做')).locator('form');
    await redo.locator('[name="reason"]').fill('请补充异常处理和权限验证的实验截图。');
    await redo.locator('[name="redo_due_at"]').fill('2030-12-03T18:00');
    await submit(redo, '确认退回');
    const extend = (await openSummary(teacher, '延长重交期限')).locator('form');
    await extend.locator('[name="reason"]').fill('补充材料需要时间，延长两天。');
    await extend.locator('[name="redo_due_at"]').fill('2030-12-05T18:00');
    await submit(extend, '延长重交期限');
    await student.goto(assignmentURL);
    assert(await student.getByText('请补充异常处理和权限验证的实验截图。', { exact: true }).count());
    await shot(student, '04-return-and-redo-deadline.png');
    await student.locator('input[type="file"]').setInputFiles({ name: '实验报告_重做完成.txt', mimeType: 'text/plain', buffer: Buffer.from('Completed report') });
    await submit(student.locator('form[enctype]'), '上传新版本');
    await teacher.goto(assignmentURL);
    await teacher.locator('form.grade [name="score"]').fill('96');
    await teacher.locator('form.grade [name="comment"]').fill('内容完整，测试与截图齐全。');
    await submit(teacher.locator('form.grade'), '保存评分');
    await teacher.goto(`${base}/gradebook?class_id=${ids.class_id}&assignment_id=${assignmentId}`);
    assert(await teacher.getByText('20260001', { exact: false }).count());
    await assertLayout(teacher, 'teacher-gradebook-desktop');
    await shot(teacher, '05-gradebook-statistics.png');
    const downloadPromise = teacher.waitForEvent('download');
    await teacher.getByRole('link', { name: '导出 CSV', exact: true }).click();
    const download = await downloadPromise;
    await download.saveAs(path.join(target, 'class-grades.csv'));
    await student.goto(`${base}/my-grades`);
    await shot(student, '06-student-grades.png');
    await teacher.goto(assignmentURL);
    const management = await openSummary(teacher, '作业管理');
    await submit(management.locator('form').last(), '归档作业');
    await student.goto(assignmentURL);
    assert.equal(await student.locator('form[enctype]').count(), 0);
    await teacher.goto(assignmentURL);
    await submit((await openSummary(teacher, '作业管理')).locator('form'), '恢复作业');
    // The separate seeded submission has already missed both deadlines.
    await student.goto(`${base}/assignment/detail/${ids.expired_id}`);
    assert.equal(await student.locator('form[enctype]').count(), 0);
    await teacher.goto(`${base}/assignment/detail/${ids.expired_id}`);
    const expiredExtend = (await openSummary(teacher, '延长重交期限')).locator('form');
    await expiredExtend.locator('[name="reason"]').fill('允许补交实验材料。');
    await expiredExtend.locator('[name="redo_due_at"]').fill('2030-12-06T18:00');
    await submit(expiredExtend, '延长重交期限');
    await student.goto(`${base}/assignment/detail/${ids.expired_id}`);
    assert.equal(await student.locator('form[enctype]').count(), 1);
    await student.goto(`${base}/assignment/list?q=分页&status=PENDING`);
    await student.getByRole('link', { name: '下一页', exact: true }).click();
    assert(student.url().includes('status=PENDING'));
    assert.equal(await student.locator('a.item').count(), 1);
    await student.setViewportSize({ width: 390, height: 844 });
    for (const route of ['/dashboard', '/assignment/list', '/notifications', '/my-grades', '/profile', `/assignment/detail/${assignmentId}`]) {
        await student.goto(base + route);
        if (route.includes('/assignment/detail/')) await openSummary(student, '历史版本');
        await assertLayout(student, `student-mobile:${route}`);
    }
    await shot(student, '07-mobile-submission.png');
    await teacher.setViewportSize({ width: 390, height: 844 });
    await teacher.goto(`${base}/gradebook?class_id=${ids.class_id}&assignment_id=${assignmentId}`);
    await assertLayout(teacher, 'teacher-gradebook-mobile');
    await shot(teacher, '08-mobile-gradebook.png');
    const anonymous = await browser.newPage({ viewport: { width: 390, height: 844 } });
    for (const route of ['/login', '/register']) {
        await anonymous.goto(base + route);
        await assertLayout(anonymous, `anonymous-mobile:${route}`);
    }
    await shot(anonymous, '09-mobile-register.png');
    assert.equal(issues.length, 0, JSON.stringify(issues));
    return { status: 'passed', checkedPages, screenshots, issues, assignmentId,
        flows: ['publish', 'edit', 'notifications', 'profile', 'versions', 'grade-history', 'return', 'extend', 'resubmit', 'gradebook', 'csv', 'archive-restore', 'expired-redo-reopen', 'pagination', 'mobile-layout'] };
}

(async () => {
    try {
        const report = await main();
        writeFileSync(path.join(target, 'browser-results.json'), JSON.stringify(report, null, 2));
        console.log(JSON.stringify({ ...report, directory: target }, null, 2));
    } catch (error) {
        writeFileSync(path.join(target, 'browser-results.json'), JSON.stringify({ status: 'failed', error: String(error), stack: error.stack, screenshots, issues }, null, 2));
        console.error(error);
        console.error(`Artifacts: ${target}`);
        process.exitCode = 1;
    } finally {
        if (browser) await browser.close();
        server.kill();
        writeFileSync(path.join(target, 'server.log'), serverLog);
    }
})();
