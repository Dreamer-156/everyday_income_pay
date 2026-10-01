// API keys remain in this password input only, never in storage or exported reports.
let preparedAnalysis = null, lastAnalysis = '', analysisRevision = 0;
const aiPresets = {
 openrouter:{model:'openrouter/free',docs:'https://openrouter.ai/openrouter/free',notice:'需要自己的 OpenRouter API Key；免费型号有额度和速率限制。发送前会重新核验零价清单，并限制输入、输出与请求单价为零。'},
 opencode:{model:'mimo-v2.5-free',docs:'https://opencode.ai/docs/zen/',notice:'OpenCode 免费层可能拒绝外部客户端；部分免费模型可能用输入改进模型。接口受限时请改选 OpenRouter，不会自动调用付费模型。免费定价由提供方决定，请核对官方规则。'}
};
function invalidateAnalysis(){analysisRevision++;preparedAnalysis=null;$('aiConsent').checked=false;$('runAnalyze').disabled=true;$('aiConsentArea').classList.add('hidden')}
function aiProviderChanged(){const spec=aiPresets[$('aiProvider').value];$('aiModel').value=spec.model;$('aiKey').value='';$('aiModels').innerHTML='';$('aiProviderNotice').textContent=spec.notice;$('aiDocs').href=spec.docs;invalidateAnalysis()}
function showAnalysis(text,meta){lastAnalysis=text;$('aiResult').textContent=text;$('aiResultMeta').textContent=meta;$('saveAnalysis').disabled=!text}
async function aiBusy(id,task){$(id).disabled=true;try{await task()}finally{$(id).disabled=id==='runAnalyze'?(!preparedAnalysis||!$('aiConsent').checked):false}}
$('aiProvider').onchange=aiProviderChanged;
$('aiModel').oninput=invalidateAnalysis;
$('month').addEventListener('change',invalidateAnalysis);
$('allMonths').addEventListener('click',invalidateAnalysis);
$('localAnalyze').onclick=()=>action(()=>aiBusy('localAnalyze',async()=>{const result=await api('analysis/local',{month:$('month').value});showAnalysis(result.analysis,'本地统计规则生成 · 没有发送任何数据 · 不修改账本')}));
$('refreshAiModels').onclick=()=>action(()=>aiBusy('refreshAiModels',async()=>{const provider=$('aiProvider').value;const result=await api('analysis/models',{provider});if(provider!==$('aiProvider').value)return;$('aiModels').innerHTML=result.models.map(model=>'<option value="'+esc(model)+'"></option>').join('');$('aiProviderNotice').textContent=result.notice+' 当前找到 '+result.models.length+' 个免费型号。';toast('免费模型列表已更新');invalidateAnalysis()}));
$('prepareAnalyze').onclick=()=>action(()=>aiBusy('prepareAnalyze',async()=>{invalidateAnalysis();const revision=analysisRevision;const result=await api('analysis/preview',{provider:$('aiProvider').value,model:$('aiModel').value.trim(),month:$('month').value});if(revision!==analysisRevision)throw Error('设置已变化，请重新预览发送内容');preparedAnalysis=result;$('aiDestination').textContent='发送至 '+preparedAnalysis.provider_name+' · '+preparedAnalysis.model+' · '+preparedAnalysis.endpoint;$('aiPayload').textContent=JSON.stringify({messages:[{role:'system',content:preparedAnalysis.system_prompt},{role:'user',content:JSON.stringify(preparedAnalysis.payload)}]},null,2);$('aiConsentArea').classList.remove('hidden')}));
$('aiConsent').onchange=()=>{$('runAnalyze').disabled=!$('aiConsent').checked||!preparedAnalysis};
$('runAnalyze').onclick=()=>action(()=>aiBusy('runAnalyze',async()=>{if(!preparedAnalysis||!$('aiConsent').checked)throw Error('请先预览并确认发送内容');if(!$('aiKey').value.trim())throw Error('请填写自己的 API Key');const consent=preparedAnalysis.consent_token;$('aiResultMeta').textContent='正在分析，请稍候（网络请求有超时限制）……';try{const result=await api('analysis/run',{consent_token:consent,confirmed:true,api_key:$('aiKey').value});showAnalysis(result.analysis,result.provider+' · '+result.actual_model+' · '+result.notice)}catch(error){$('aiResultMeta').textContent=error.message;throw error}finally{$('aiKey').value='';invalidateAnalysis()}}));
$('saveAnalysis').onclick=()=>{if(!lastAnalysis)return;const blob=new Blob([lastAnalysis],{type:'text/plain;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='账单分析-'+new Date().toISOString().slice(0,10)+'.txt';link.click();setTimeout(()=>URL.revokeObjectURL(url),15000)};
aiProviderChanged();
