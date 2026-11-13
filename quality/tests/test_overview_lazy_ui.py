"""实际运行页面脚本，验证概况按需加载和缓存失效竞态。"""

import json
import re
import subprocess

import pytest

from quality.tests.test_admin_ui import ADMIN_PAGE, EXPORTS, HARNESS, NODE


CASES = {
    "collapsed_query_cache_force_and_failure": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
node('qualityOverviewPanel').open=false;
ui.applyQualityFilters();
assert.equal(overviewRequests.length,0);
respond(0,{items:[],total:0,page:1}); await tick();
node('qualityOverviewPanel').open=true; node('qualityOverviewPanel').handlers.toggle();
assert.equal(overviewRequests.length,1);
respondTo(overviewRequests,0,{total_rounds:7,participating_agents:[]}); await tick();
node('qualityOverviewPanel').open=false; node('qualityOverviewPanel').handlers.toggle();
node('qualityOverviewPanel').open=true; node('qualityOverviewPanel').handlers.toggle();
assert.equal(overviewRequests.length,1); assert.equal(ui.qualityState.overview.total_rounds,7);
node('qualityOverviewPanel').open=false; node('qualityOverviewPanel').handlers.toggle();
ui.applyQualityFilters(); respond(1,{items:[],total:0,page:1}); await tick();
assert.equal(overviewRequests.length,1);
node('qualityOverviewPanel').open=true; node('qualityOverviewPanel').handlers.toggle();
assert.equal(overviewRequests.length,2);
respondTo(overviewRequests,1,{detail:'failed'},503); await tick();
node('qualityOverviewPanel').open=false; node('qualityOverviewPanel').handlers.toggle();
node('qualityOverviewPanel').open=true; node('qualityOverviewPanel').handlers.toggle();
assert.equal(overviewRequests.length,3);
respondTo(overviewRequests,2,{total_rounds:8,participating_agents:[]}); await tick();
node('qualityOverviewPanel').open=false; node('qualityOverviewPanel').handlers.toggle();
node('qualityKeyword').value='changed'; ui.applyQualityFilters();
respond(2,{items:[],total:0,page:1}); await tick();
assert.equal(overviewRequests.length,3);
node('qualityOverviewPanel').open=true; node('qualityOverviewPanel').handlers.toggle();
assert.equal(overviewRequests.length,4);
assert.equal(new URL(overviewRequests[3].url).searchParams.get('keyword'),'changed');
respondTo(overviewRequests,3,{total_rounds:9,participating_agents:[]}); await tick();
""",
    "review_save_invalidates_inflight_result_even_when_collapsed": r"""
ui.state.token='session'; ui.state.view='quality'; ui.qualityState.ready=true;
node('qualityOverviewPanel').open=true;
const older=ui.loadQualityOverview();
node('qualityOverviewPanel').open=false;
const round={round_id:'r1',reception_type:'human',review:{ai_status:'passed',human_status:'passed',revision:'1'}};
ui.setQualityReview(round,round.review);
const save=ui.saveQualityReview('qualityReviewBothFailed');
respond(0,{ai_status:'failed',human_status:'failed',remark:'',revision:'2'}); await save;
assert.equal(overviewRequests[0].options.signal.aborted,true);
respondTo(overviewRequests,0,{total_rounds:999,participating_agents:[]}); await older;
assert.equal(ui.qualityState.overviewCache,null);
node('qualityOverviewPanel').open=true; node('qualityOverviewPanel').handlers.toggle();
assert.equal(overviewRequests.length,2);
respondTo(overviewRequests,1,{total_rounds:1,participating_agents:[]}); await tick();
assert.equal(ui.qualityState.overview.total_rounds,1);
const again=ui.saveQualityReview('qualityReviewAllPassed');
respond(1,{ai_status:'passed',human_status:'passed',remark:'',revision:'3'}); await again;
assert.equal(overviewRequests.length,3);
respondTo(overviewRequests,2,{total_rounds:2,participating_agents:[]}); await tick();
assert.equal(ui.qualityState.overview.total_rounds,2);
""",
}


@pytest.mark.skipif(NODE is None, reason="需要 Node.js 运行页面脚本")
@pytest.mark.parametrize("case", CASES)
def test_lazy_overview(case):
    script = re.search(r"<script>(.*?)</script>", ADMIN_PAGE.read_text(encoding="utf-8"), re.S).group(1)
    program = (HARNESS + "\nvm.runInContext(" + json.dumps(script + EXPORTS)
               + ", context); const ui=context.ui;\n(async()=>{\n" + CASES[case]
               + "\n})().catch(error=>{console.error(error);process.exitCode=1;});")
    result = subprocess.run([NODE, "-"], input=program, text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
