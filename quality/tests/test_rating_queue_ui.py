"""验证评分复核的完整内容同步、竞态和独立草稿，以及排队配置提交。"""
import json
import re
import subprocess
import pytest
from quality.tests.test_admin_ui import ADMIN_PAGE, HARNESS, NODE

CASES = {
 'rating_save_preserves_draft_and_ignores_old_read': r'''
ui.state.token='session';ui.state.view='quality';ui.qualityState.ready=true;
const value={round_id:'r1',status:'unmarked',revision:'0',content_revision:'a',rating_status:'rated',rating:2,rating_feedback:'旧',selected_rating:{invitation_message_id:7}};
const round={round_id:'r1',reception_type:'human',rating_review:value,...value};
ui.qualityState.detail={type:'round',id:'r1',page:1,item:round};ui.setQualityReview(round);
node('qualityReviewRemark').value='草稿';node('qualityReviewRemark').handlers.input();
const refresh=ui.refreshQualityRatings();
const saving=ui.saveQualityRatingReview('invalid');
assert.deepEqual(JSON.parse(requests[1].options.body),{status:'invalid',revision:'0',content_revision:'a'});
respond(1,{...value,status:'invalid',revision:'9',rating_feedback:'新',content_revision:'b'});await saving;
respond(0,{round:{...round,rating_review:value},messages:[]});await refresh;
assert.equal(ui.qualityState.ratingReview.value.status,'invalid');
assert.equal(ui.qualityState.detail.item.rating_feedback,'新');
assert.equal(node('qualityReviewRemark').value,'草稿');
assert.equal(node('qualityRatingInvalid').attributes['aria-pressed'],'true');
''',
 'unrated_and_selected_are_not_submitted': r'''
ui.state.token='session';ui.state.view='quality';ui.qualityState.ready=true;
ui.setQualityReview({round_id:'r1',rating_review:{round_id:'r1',status:'unmarked',revision:'0',content_revision:'a',rating_status:'pending',rating:null}});
await ui.saveQualityRatingReview('invalid');assert.equal(requests.length,0);assert.equal(node('qualityRatingInvalid').disabled,true);
''',
 'queue_save_protocol': r'''
ui.state.token='session';ui.state.view='handoff-queue';
const p=ui.loadHandoffQueue();respond(0,{version:4,config:{enabled:false,daily_tiers:[{start:0,min_count:0,max_count:0}],weekly_tiers:[],step_min_s:10,step_max_s:20,estimate_per_person_s:30}});await tick();respond(1,{items:[]});await p;
node('queueEnabled').checked=true;const save=ui.saveHandoffQueue();
assert.equal(JSON.parse(requests[2].options.body).expected_version,4);assert.equal(JSON.parse(requests[2].options.body).config.enabled,true);
respond(2,{version:5,config:{...ui.handoffQueueState.config,enabled:true}});await tick();respond(3,{items:[]});await save;
assert.match(node('queueStatus').textContent,/已生效/);
'''
}
CASES['selected_bubble_matches_invitation_only'] = r"""
ui.state.token='session';ui.state.view='quality';ui.qualityState.ready=true;
const v={round_id:'r1',status:'invalid',revision:'1',content_revision:'a',rating_status:'rated',rating:2,selected_rating:{invitation_message_id:7}};
ui.setQualityReview({round_id:'r1',rating_review:v});
const message={round_id:'r1',message_id:7,visitor_rating:{rating:2}};
assert.match(ui.qualityMessageRating(message),/已复核标记无效/);
assert.doesNotMatch(ui.qualityMessageRating({...message,message_id:8}),/已复核标记无效/);
assert.doesNotMatch(ui.qualityMessageRating({...message,round_id:'r2'}),/已复核标记无效/);
"""
CASES['saving_then_switch_round_keeps_new_panel'] = r"""
ui.state.token='session';ui.state.view='quality';ui.qualityState.ready=true;
const v={round_id:'r1',status:'unmarked',revision:'0',content_revision:'a',rating_status:'rated',rating:2};
const r={round_id:'r1',rating_review:v};ui.qualityState.items=[r];ui.setQualityReview(r);
const saving=ui.saveQualityRatingReview('invalid');
ui.setQualityReview({round_id:'r2',rating_review:{...v,round_id:'r2',rating:5}});
respond(0,{...v,status:'invalid',revision:'1'});await saving;
assert.equal(ui.qualityState.ratingReview.roundId,'r2');assert.equal(ui.qualityState.ratingReview.value.rating,5);
assert.equal(r.rating_review.status,'invalid');
"""
CASES['conflict_read_failure_disables_submit_until_retry'] = r"""
ui.state.token='session';ui.state.view='quality';ui.qualityState.ready=true;
const v={round_id:'r1',status:'unmarked',revision:'0',content_revision:'a',rating_status:'rated',rating:2};ui.setQualityReview({round_id:'r1',rating_review:v});
const save=ui.saveQualityRatingReview('invalid');respond(0,{detail:'已被他人修改'},409);await tick();
assert.equal(node('qualityRatingInvalid').disabled,true);respond(1,{detail:'读取失败'},503);await save;
assert.equal(node('qualityRatingInvalid').disabled,true);await ui.saveQualityRatingReview('invalid');assert.equal(requests.length,2);
const retry=ui.loadQualityRatingReview();respond(2,{...v,status:'valid',revision:'2',content_revision:'b',rating:5});await retry;
assert.equal(ui.qualityState.ratingReview.value.rating,5);assert.equal(node('qualityRatingInvalid').disabled,false);
"""
CASES.update({
'same_round_read_stale': r'''
ui.state.token='session';ui.state.view='quality';ui.qualityState.ready=true;
const v={round_id:'r1',status:'unmarked',revision:'0',content_revision:'a',rating_status:'rated',rating:2,rating_feedback:'旧'};
const r={round_id:'r1',conversation_id:1,round_no:1,reception_type:'human',rating_review:v,...v};
ui.qualityState.detail={type:'round',id:'r1',page:1,item:r};ui.setQualityReview(r);
const p=ui.loadQualityDetail();respond(0,{round:{...r,rating:5,rating_feedback:'新',rating_review:{...v,rating:5,rating_feedback:'新',content_revision:'b',revision:'2',status:'valid'}},messages:[],total:1,page:1});await p;
console.log(JSON.stringify({detailRating:ui.qualityState.detail.item.rating,panel:node('qualityRatingContent').textContent,panelRevision:ui.qualityState.ratingReview.value.content_revision}));
assert.equal(ui.qualityState.ratingReview.value.content_revision,'b');
''',
'pending_total_reappears':r'''
ui.state.token='session';ui.state.view='quality';ui.qualityState.ready=true;ui.qualityState.applied=new URLSearchParams('metric=low_rating');ui.qualityState.total=9;
const v={round_id:'r1',status:'unmarked',revision:'0',content_revision:'a',rating_status:'rated',rating:2};
const r={round_id:'r1',conversation_id:1,round_no:1,reception_type:'human',rating_review:v,...v};ui.qualityState.detail={type:'round',id:'r1',page:1,item:r};ui.setQualityReview(r);
const save=ui.saveQualityRatingReview('invalid');respond(0,{...v,status:'invalid',revision:'1'});await tick();respond(1,{detail:'网络错误'},503);await save;
console.log('after save failure:',node('qualityListSummary').textContent);
const refresh=ui.refreshQualityRatings();respond(2,{round:{...r,rating_review:{...v,status:'invalid',revision:'1'}},messages:[]});await refresh;
console.log('after periodic refresh:',node('qualityListSummary').textContent);
assert.match(node('qualityListSummary').textContent,/待刷新/);
''',
'metric_remove_restore':r'''
ui.state.token='session';ui.state.view='quality';ui.qualityState.ready=true;ui.qualityState.applied=new URLSearchParams('metric=low_rating');ui.qualityState.total=1;
const v={round_id:'r1',status:'unmarked',revision:'0',content_revision:'a',rating_status:'rated',rating:2};const r={round_id:'r1',conversation_id:1,round_no:1,reception_type:'human',rating_review:v,...v};ui.qualityState.detail={type:'round',id:'r1',page:1,item:r};ui.setQualityReview(r);
node('qualityReviewRemark').value='草稿';node('qualityReviewRemark').handlers.input();
let p=ui.saveQualityRatingReview('invalid');respond(0,{...v,status:'invalid',revision:'1'});await tick();respond(1,{items:[],total:0,page:1});await tick();respond(2,{items:[],total:0,page:1});await p;
assert.equal(ui.qualityState.ratingReview.outside,true);assert.equal(ui.qualityState.detail.id,'r1');assert.equal(node('qualityReviewRemark').value,'草稿');assert.equal(ui.qualityState.total,0);
p=ui.saveQualityRatingReview('valid');respond(3,{...v,status:'valid',revision:'2'});await tick();respond(4,{items:[{customer_id:'c1',latest_round:r}],total:1,page:1,filter_scoped:true});await tick();respond(5,{items:[r],total:1,page:1});await p;
assert.equal(ui.qualityState.ratingReview.outside,false);assert.equal(ui.qualityState.total,1);assert.equal(node('qualityReviewRemark').value,'草稿');console.log('remove and restore passed');
'''
})

@pytest.mark.skipif(NODE is None, reason='需要 Node.js')
@pytest.mark.parametrize('case', CASES)
def test_rating_queue_ui(case):
    script=re.search(r'<script>(.*?)</script>',ADMIN_PAGE.read_text(encoding='utf-8'),re.S).group(1)
    exports='\nglobalThis.ui={state,qualityState,setQualityReview,qualityMessageRating,loadQualityDetail,refreshQualityRatings,saveQualityRatingReview,loadQualityRatingReview,loadHandoffQueue,saveHandoffQueue,handoffQueueState};'
    program=HARNESS+'\nvm.runInContext('+json.dumps(script+exports)+',context);const ui=context.ui;\n(async()=>{'+CASES[case]+'})().catch(e=>{console.error(e);process.exitCode=1;});'
    result=subprocess.run([NODE,'-'],input=program,text=True,encoding='utf-8',capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
