"""Execute the real picker handlers: one server-owned decision per paid selection."""
from __future__ import annotations

import json

import pytest

from core.price_safety_fragment import _GATE_JS
from tests.chat_page_js_harness import DOM, run_node, script

# The shared DOM does not parse innerHTML. Materialize only this fragment's input
# values and saved-bound marker from the real rendered markup; do not replace its
# validation, HTTP checks, save handlers or decision resolution.
_GATE_FIXTURE = r"""
function gateBody(){ return document.body.children.find(x=>x.id==='vgOverlay')?.querySelector('#vgBody'); }
const getById=document.getElementById.bind(document), query=document.querySelector.bind(document);
document.getElementById=id=>{
  const overlay=document.body.children.find(x=>x.id==='vgOverlay');
  if(overlay && ['vgActions','vgBody','vgHead','vgModal'].includes(id)) return overlay.querySelector('#'+id);
  if(id!=='vgMaxIn' && id!=='vgMaxOut') return getById(id);
  const html=gateBody()?.innerHTML || '', match=html.match(new RegExp('<input id="'+id+'"[^>]*value="([^"]*)"'));
  if(!match) return null;
  const el=getById(id);
  if(el.renderedMarkup!==html){ el.value=el.defaultValue=match[1]; el.renderedMarkup=html; }
  return el;
};
document.querySelector=selector=>selector==='.vg-max[data-has-bound="1"]'
  ? (gateBody()?.innerHTML.includes('data-has-bound="1"') ? query(selector) : null) : query(selector);
const requests=[], writes=[];
const gateModel={id:'demo',prompt_usd_per_m:1,completion_usd_per_m:6};
const approvedRoutes={demo:{max_input_microunits_per_million:1000000,max_output_microunits_per_million:6000000}};
async function gateFetch(url,opts={}){
  requests.push(url);
  if(opts.method==='POST'){ writes.push({url,body:JSON.parse(opts.body)}); throw Error('unexpected write'); }
  if(url==='/api/cloud/acceptances') return Response.json({acceptances:[]});
  if(url==='/api/cloud/usepod/discovery') return Response.json({approved_routes:approvedRoutes});
  if(url.startsWith('/api/cloud/models?')) return Response.json({provider:'usepod',age_seconds:0,models:[gateModel]});
  throw Error('unexpected read: '+url);
}
fetch=gateFetch;
"""


def _gate_program(setup: str, drive: str) -> str:
    return DOM + _GATE_FIXTURE + setup + _GATE_JS + drive


@pytest.mark.parametrize("provider,row_free,server_paid,allow", [
    ("usepod", False, True, True),
    ("openrouter", False, True, True),
    ("usepod", False, True, False),
    ("usepod", True, True, True),  # stale free label cannot bypass the server
    ("openrouter", True, False, True),
])
def test_row_selection_has_one_server_owned_confirmation(provider, row_free, server_paid, allow):
    config = json.dumps(dict(provider=provider, row_free=row_free, server_paid=server_paid, allow=allow))
    result = run_node(DOM + script() + "\nawait new Promise(r=>setImmediate(r));\nconst cfg=" + config + ";\n" + r"""
setDisplayedChat('openclaw:abababababababababab'); setModelValue('vool');
const decisions=[], posts=[];
window.VoolPriceGate={review:async ctx=>{decisions.push(ctx); return cfg.allow;}};
fetch=async (url,opts)=>{
  if (!opts || opts.method!=='POST') return {ok:true,json:async()=>({ok:true})};
  const body=JSON.parse(opts.body); posts.push(body);
  if(cfg.server_paid && !body.confirm_paid)
    return {ok:false,status:409,json:async()=>({code:'paid_model_confirm_required'})};
  return {ok:true,status:200,json:async()=>({ok:true,provider:cfg.provider,model:'demo-model'})};
};
const row=makeCloudRow({provider:cfg.provider,id:'demo-model',name:'Demo',free:cfg.row_free});
await row.__on.click({stopPropagation(){}});
out({decisions,posts,selected:modelValue,acked:paidPinAcked(modelValue)});
""")
    assert result["errors"] == []
    assert result["acked"] == (server_paid and allow)
    assert len(result["decisions"]) == int(server_paid)
    assert "confirm_paid" not in result["posts"][0]
    if server_paid:
        assert result["decisions"][0]["code"] == "paid_model_confirm_required"
    if server_paid and not allow:
        assert len(result["posts"]) == 1
        assert result["selected"] == "vool"
    else:
        assert len(result["posts"]) == (2 if server_paid else 1)
        if server_paid:
            assert result["posts"][1]["confirm_paid"] is True
        assert result["selected"] == ("usepod:" if provider == "usepod" else "") + "demo-model"


def test_stale_prices_refresh_before_the_single_confirmation_click():
    result = run_node(_gate_program(r"""
let releaseRefresh;
fetch=async (url,opts)=>{
  if(url.includes('refresh=1')){ requests.push(url); return await new Promise(r=>releaseRefresh=r); }
  if(url.startsWith('/api/cloud/models?')){
    requests.push(url);
    return Response.json({provider:'usepod',age_seconds:1200,models:[{...gateModel,completion_usd_per_m:2}]});
  }
  return gateFetch(url,opts);
};
""", r"""
const decision=window.VoolPriceGate.review({kind:'pin',id:'usepod:demo',provider:'usepod'});
await new Promise(r=>setImmediate(r));
const overlay=document.body.children.find(x=>x.id==='vgOverlay');
const actions=overlay.querySelector('#vgActions');
const automatic=Boolean(releaseRefresh), disabledBefore=actions.children[0].disabled;
if(!automatic){out({automatic,disabledBefore});}else{
  releaseRefresh(Response.json({provider:'usepod',age_seconds:0,models:[{id:'demo',prompt_usd_per_m:3,completion_usd_per_m:4}]}));
  await new Promise(r=>setImmediate(r));
  const beforeClick=overlay.querySelector('#vgBody').innerHTML;
  const enabledAfter=!actions.children[0].disabled;
  actions.children[0].click();
  out({automatic,disabledBefore,enabledAfter,beforeClick,accepted:await decision,hidden:overlay.hidden,requests,writes});
}
"""))
    assert result["automatic"] is True
    assert result["disabledBefore"] is True
    assert result["enabledAfter"] is True
    assert "3.00 USDC" in result["beforeClick"] and "4.00 USDC" in result["beforeClick"]
    assert result["accepted"] == "conversation" and result["hidden"] is True
    assert sum("refresh=1" in url for url in result["requests"]) == 1
    assert result["writes"] == []  # A price acknowledgement cannot widen saved route limits.
    assert result["errors"] == []


def test_conversation_ack_is_bounded_scoped_and_once_really_is_once():
    result = run_node(DOM + script() + "\nawait new Promise(r=>setImmediate(r));\n" + r"""
const a='openclaw:aaaaaaaaaaaaaaaaaaaa', b='openclaw:bbbbbbbbbbbbbbbbbbbb';
setDisplayedChat(a); setModelValue('usepod:astra');
acknowledgePaidPin(a,'usepod:astra','conversation');
consumePaidPinAck(a,'usepod:astra');
const retained=paidPinAcked('usepod:astra',a);
const otherChat=paidPinAcked('usepod:astra',b), otherProvider=paidPinAcked('openrouter:astra',a);
setModelValue('usepod:other');setModelValue('usepod:astra');
const afterSwitch=paidPinAcked('usepod:astra',a);
acknowledgePaidPin(a,'usepod:astra',true);
const onceBefore=paidPinAcked('usepod:astra',a);
consumePaidPinAck(a,'usepod:astra');const onceAfter=paidPinAcked('usepod:astra',a);
acknowledgePaidPin(a,'usepod:astra','conversation');
const now=Date.now;Date.now=()=>now()+86400001;  // conversation acks now last 24h
const expired=paidPinAcked('usepod:astra',a);
out({retained,otherChat,otherProvider,afterSwitch,onceBefore,onceAfter,expired});
""")
    assert result["retained"] and result["onceBefore"]
    assert not any(result[k] for k in ['otherChat','otherProvider','afterSwitch','onceAfter','expired'])


def test_price_toggle_only_repaints_matching_rows_without_catalogue_fetch():
    result = run_node(DOM + script() + "\nawait new Promise(r=>setImmediate(r));\n" + r"""
let paints=0, requests=0;fetch=async()=>{requests++;throw Error('unexpected fetch');};
const toolbar=buildCloudToolbar(()=>paints++);
const label=toolbar.children[toolbar.children.length-1];const checkbox=label.children[0];
checkbox.checked=true;checkbox.__on.change({stopPropagation(){}});
out({paints,requests,showPrices:cloudShowPrices});
""")
    assert result["paints"] == 1 and result["requests"] == 0 and result["showPrices"]


@pytest.mark.parametrize("kind", ["pin", "per-send"])
def test_gate_offers_a_conversation_decision_with_explicit_limits(kind):
    result = run_node(_gate_program("\nconst kind=" + json.dumps(kind) + ";\n", r"""
const decision=window.VoolPriceGate.review({kind,id:'usepod:demo',provider:'usepod'});
await new Promise(r=>setImmediate(r));
const overlay=document.body.children.find(x=>x.id==='vgOverlay');
const actions=overlay.querySelector('#vgActions');
const button=actions.children.find(x=>String(x.className||'').split(' ').includes('vg-conversation'));
const label=button.textContent;
const body=overlay.querySelector('#vgBody').innerHTML;
button.click();out({decision:await decision,body,hidden:overlay.hidden,label,writes});
"""))
    assert result["decision"] == "conversation" and result["hidden"]
    assert result["label"] == "Accept price for this chat (24 hours)"
    assert "24 hours" in result["body"] and "spending limits" in result["body"]
    # Price acceptance does not widen the separately approved monetary budget.
    assert "price only" in result["body"] and "approved UsePod budget" in result["body"]
    assert "Provider <b>UsePod</b>" in result["body"] and "6.00 USDC" in result["body"]
    assert result["writes"] == []
    assert result["errors"] == []


def test_late_acceptance_read_cannot_replace_enabled_confirmation_buttons():
    result = run_node(_gate_program(r'''
const acceptanceReads=[];
fetch=async (url,opts)=>{
  if(url.includes('/acceptances')){ requests.push(url); return new Promise(resolve=>acceptanceReads.push(resolve)); }
  return gateFetch(url,opts);
};
''', r'''
const decision=window.VoolPriceGate.review({kind:'pin',id:'usepod:demo',provider:'usepod'});
await new Promise(r=>setImmediate(r));
const overlay=document.body.children.find(x=>x.id==='vgOverlay');
const actions=overlay.querySelector('#vgActions');
const before=actions.children[0].disabled;
acceptanceReads.forEach(resolve=>resolve(Response.json({acceptances:[]})));
await new Promise(r=>setImmediate(r));
const button=actions.children[0];
await new Promise(r=>setImmediate(r));
const unchanged=button===actions.children[0], enabled=!button.disabled;
button.click();out({before,unchanged,enabled,decision:await decision,writes});
'''))
    assert result['before'] and result['unchanged'] and result['enabled']
    assert result['decision'] == 'conversation'
    assert result['writes'] == []
    assert result['errors'] == []


@pytest.mark.parametrize("failure", ["catalog", "acceptances", "bounds", "identity"])
def test_failed_review_read_cannot_enable_confirmation_or_write(failure):
    setup = "\nconst failure=" + json.dumps(failure) + ";\n" + r"""
fetch=async (url,opts)=>{
  const target=failure==='acceptances' ? url==='/api/cloud/acceptances'
    : failure==='bounds' ? url==='/api/cloud/usepod/discovery' : url.startsWith('/api/cloud/models?');
  if(target){
    requests.push(url);
    return failure==='identity'
      ? Response.json({provider:'openrouter',age_seconds:0,models:[gateModel]})
      : Response.json({error:'synthetic read failure'},{status:503});
  }
  return gateFetch(url,opts);
};
"""
    result = run_node(_gate_program(setup, r"""
let settled=false;
const decision=window.VoolPriceGate.review({kind:'pin',id:'usepod:demo',provider:'usepod'});
decision.then(()=>settled=true);
await new Promise(r=>setImmediate(r));
const overlay=document.body.children.find(x=>x.id==='vgOverlay'), actions=document.getElementById('vgActions');
actions.children[0].click(); actions.children[1].click();
const blocked={disabled:actions.children.slice(0,2).every(x=>x.disabled),open:!overlay.hidden,settled,
  error:document.getElementById('vgError').textContent,writes:writes.slice()};
actions.children[3].click();
out({blocked,decision:await decision,hidden:overlay.hidden,writes});
"""))
    assert result["errors"] == []
    assert result["blocked"]["disabled"] and result["blocked"]["open"]
    assert not result["blocked"]["settled"] and result["blocked"]["writes"] == []
    expected = "identity mismatch" if failure == "identity" else "Could not read price information"
    assert expected in result["blocked"]["error"]
    assert result["decision"] is False and result["hidden"] and result["writes"] == []


@pytest.mark.parametrize("button", [0, 1])
@pytest.mark.parametrize("status", [200, 503])
def test_reviewed_maxima_save_is_exact_and_failure_retains_the_draft(button, status):
    setup = "\nconst button=" + str(button) + ", status=" + str(status) + ";\n" + r"""
fetch=async (url,opts)=>{
  if(url==='/api/cloud/usepod/approve-route' && opts?.method==='POST'){
    writes.push({url,body:JSON.parse(opts.body)});
    if(status===200) approvedRoutes.demo={max_input_microunits_per_million:1250000,max_output_microunits_per_million:6500000};
    return Response.json(status===200 ? {ok:true} : {error:'synthetic save refusal'},{status});
  }
  return gateFetch(url,opts);
};
"""
    result = run_node(_gate_program(setup, r"""
let settled=false;
const decision=window.VoolPriceGate.review({kind:'pin',id:'usepod:demo',provider:'usepod'});
decision.then(()=>settled=true);
await new Promise(r=>setImmediate(r));
const overlay=document.body.children.find(x=>x.id==='vgOverlay'), actions=document.getElementById('vgActions');
document.getElementById('vgMaxIn').value='1.25'; document.getElementById('vgMaxOut').value='6.5';
actions.children[button].click();
await new Promise(r=>setImmediate(r));
const after={open:!overlay.hidden,settled,enabled:!actions.children[button].disabled,
  input:document.getElementById('vgMaxIn').value,output:document.getElementById('vgMaxOut').value,
  error:document.getElementById('vgError').textContent};
if(status!==200) actions.children[3].click();
out({after,decision:await decision,writes,requests});
"""))
    assert result["errors"] == []
    assert result["writes"] == [{"url": "/api/cloud/usepod/approve-route", "body": {
        "model_id": "demo", "max_input_usdc_per_million": "1.25", "max_output_usdc_per_million": "6.5",
    }}]  # Saving reviewed maxima sends no chat, payment, or model-selection request.
    assert sum(url == "/api/cloud/usepod/discovery" for url in result["requests"]) == (2 if status == 200 else 1)
    if status == 200:
        assert not result["after"]["open"] and result["after"]["settled"]
        assert result["decision"] == "conversation" and result["after"]["error"] == ""
    else:
        assert result["after"]["open"] and not result["after"]["settled"]
        assert result["after"]["enabled"] and result["after"]["error"] == "synthetic save refusal"
        assert result["after"]["input"] == "1.25" and result["after"]["output"] == "6.5"
        assert result["decision"] is False


@pytest.mark.parametrize("invalid", ["0", "-1", "NaN", "1.1234567"])
def test_invalid_reviewed_maximum_cannot_save_or_approve(invalid):
    setup = "\nconst invalid=" + json.dumps(invalid) + ";\n"
    result = run_node(_gate_program(setup, r"""
let settled=false;
const decision=window.VoolPriceGate.review({kind:'pin',id:'usepod:demo',provider:'usepod'});
decision.then(()=>settled=true);
await new Promise(r=>setImmediate(r));
const overlay=document.body.children.find(x=>x.id==='vgOverlay'), actions=document.getElementById('vgActions');
document.getElementById('vgMaxIn').value=invalid;
actions.children[0].click(); actions.children[1].click();
const after={open:!overlay.hidden,settled,input:document.getElementById('vgMaxIn').value,
  error:document.getElementById('vgError').textContent,writes:writes.slice()};
actions.children[3].click();
out({after,decision:await decision,writes});
"""))
    assert result["errors"] == []
    assert result["after"]["open"] and not result["after"]["settled"]
    assert result["after"]["input"] == invalid
    assert "Give both maximum prices as positive numbers" in result["after"]["error"]
    assert result["after"]["writes"] == [] and result["writes"] == []
    assert result["decision"] is False


def test_rapid_double_click_on_one_model_row_creates_one_decision():
    result = run_node(DOM + script() + '\nawait new Promise(r=>setImmediate(r));\n' + r'''
setDisplayedChat('openclaw:abababababababababab');setModelValue('vool');
let resolveDecision, decisions=0;const posts=[];
window.VoolPriceGate={review:()=>{decisions++;return new Promise(r=>resolveDecision=r);}};
fetch=async (url,opts)=>{
 if(!opts || opts.method!=='POST')return {ok:true,json:async()=>({})};
 const b=JSON.parse(opts.body);posts.push(b);
 return {ok:!!b.confirm_paid,status:b.confirm_paid?200:409,json:async()=>b.confirm_paid?{ok:true,provider:'usepod',model:'demo'}:{code:'paid_model_confirm_required'}};
};
const row=makeCloudRow({provider:'usepod',id:'demo',name:'Demo',free:false});
const event={stopPropagation(){}};
const first=row.__on.click(event), second=row.__on.click(event);
await new Promise(r=>setImmediate(r));const disabled=row.disabled;
resolveDecision(true);await Promise.all([first,second]);
out({decisions,posts:posts.length,disabled,selected:modelValue});
''')
    assert result['decisions']==1 and result['posts']==2 and result['disabled']
    assert result['selected']=='usepod:demo'


def test_chat_provider_badge_ignores_global_provider_and_stale_response():
    result = run_node(DOM + script() + '\nawait new Promise(r=>setImmediate(r));\n' + r'''
setDisplayedChat('openclaw:abababababababababab');
const pending=[], urls=[];
fetch=async url=>{urls.push(url);return new Promise(resolve=>pending.push(resolve));};
setModelValue('usepod:demo');
renderConnections([{id:'cloud',provider:'openrouter',label:'OpenRouter',state:'ok'}]);
const label=()=>cloudPillEl.children.map(n=>n.children.map(c=>c.textContent).join('')).join('');
const initial=label();
setModelValue('openai:demo');
pending[0]({json:async()=>({connections:[{id:'cloud',provider:'usepod',label:'UsePod',state:'ok'}]})});
await new Promise(r=>setImmediate(r));
const afterStale=label();
out({initial,afterStale,urls});
''')
    assert result['initial']=='UsePod' and result['afterStale']=='openai'
    assert any('provider=usepod' in u for u in result['urls'])
    assert any('provider=openai' in u for u in result['urls'])
