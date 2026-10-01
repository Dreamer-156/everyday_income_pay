const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value:'', checked:false, disabled:false, textContent:'', innerHTML:'', href:'',
    classList:{hidden:true, add(){this.hidden=true}, remove(){this.hidden=false}},
    addEventListener(){}
  });
  return elements.get(id);
}
const pending = [];
const errors = [];
const context = vm.createContext({
  $:element, esc:String, toast(){}, setTimeout, Blob, URL, Date,
  document:{createElement(){return {click(){}}}},
  api:(route,body)=>new Promise(resolve=>pending.push({route,body,resolve})),
  action:async task=>{try {await task()}catch(error){errors.push(error.message)}}
});
element('aiProvider').value='openrouter';
element('month').value='2026-10';
vm.runInContext(fs.readFileSync(path.join(__dirname,'..','analysis-ui.js'),'utf8'),context);
async function check() {
  let work=element('prepareAnalyze').onclick();
  assert.equal(pending[0].body.provider,'openrouter');
  element('aiProvider').value='opencode';element('aiProvider').onchange();
  pending.shift().resolve({consent_token:'stale',provider_name:'OpenRouter',model:'openrouter/free',endpoint:'old',payload:{},system_prompt:'fixture'});
  await work;
  assert.equal(element('aiConsentArea').classList.hidden,true);
  assert.equal(element('runAnalyze').disabled,true);
  assert.ok(errors.some(message=>message.includes('设置已变化')));

  work=element('refreshAiModels').onclick();
  assert.equal(pending[0].body.provider,'opencode');
  element('aiProvider').value='openrouter';element('aiProvider').onchange();
  pending.shift().resolve({models:['mimo-v2.5-free'],notice:'old provider'});
  await work;
  assert.equal(element('aiModels').innerHTML,'');
  assert.ok(element('aiProviderNotice').textContent.includes('OpenRouter'));
  console.log('PASS: stale previews and catalogs cannot cross provider changes');
}
check().catch(error=>{console.error(error);process.exitCode=1});
