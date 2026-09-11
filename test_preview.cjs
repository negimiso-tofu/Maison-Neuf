// Synthetic status data and a minimal DOM; no browser, network, or dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function main(){
  const timers = [], frames = [];
  function element(){
    const classes = new Set();
    return {style:{}, dataset:{}, children:[], textContent:'',
      classList:{add:name=>classes.add(name), remove:name=>classes.delete(name)},
      appendChild(child){ this.children.push(child); },
      replaceChildren(...children){ this.children = children; },
      focus(){},
      querySelector(){ return this; },
      setAttribute(key,value){ this[key] = value; }};
  }
  const elements = {stage:element(), connection:element()};
  const sandbox = {console, Date, AbortController,
    document:{getElementById:id=>(elements[id] ||= element()), createElement:element},
    setTimeout:(fn,ms)=>{ timers.push({fn,ms}); return timers.length; },
    clearTimeout:()=>{}, requestAnimationFrame:fn=>frames.push(fn),
    fetch:async()=>{ throw new Error('missing'); }};
  vm.createContext(sandbox);
  const code = fs.readFileSync(path.join(__dirname, 'preview.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(code, sandbox);
  const run = code => vm.runInContext(code, sandbox);
  await run('pollStatus()');
  assert.equal(run('liveMode'), false);
  assert.match(elements.connection.textContent, /デモ表示/);
  assert.match(elements.connection.textContent, /サーバーに接続できません/);
  sandbox.payload = JSON.parse(run(`JSON.stringify({updatedAt:new Date().toISOString(), agents:
    Object.fromEntries(ROSTER.map((def,i)=>[def.id,{state:['working','idle','away'][i%3],
    lastSeen:new Date().toISOString(),detail:'Read'}]))})`));
  run('applyStatus(validateStatus(payload))');
  assert.equal(run('liveMode'), true);
  assert.equal(run('agents.clarice.stateLabel.textContent'), '稼働中');
  assert.equal(run('agents.verity.stateLabel.textContent'), '待機中');
  assert.equal(run('agents.aurelia.spr.dataset.state'), 'away');
  assert.equal(run('agents.clarice.spr.children[1].src'), 'Clarice_01_working_48x48.png');
  assert.equal(elements['workers-body'].children.length, 9);
  run('agents.clarice.spr.onclick()');
  assert.equal(elements['card-name'].textContent, 'クラリス');
  assert.match(elements['card-skills'].textContent, /Claude Code/);
  assert.match(elements['card-state'].textContent, /稼働中/);
  sandbox.payload.tasks = [{skill:'<img src=x onerror=bad()>',timestamp:new Date().toISOString()}];
  sandbox.payload.artifacts = [{filename:'report.pdf',timestamp:new Date().toISOString(),kind:'created'}];
  run('applyStatus(payload)');
  assert.equal(elements['task-history'].children[0].children[0].textContent, '<img src=x onerror=bad()>');
  assert.match(elements['artifact-history'].children[0].children[0].textContent, /report.pdf · 新規/);
  elements['card-close'].onclick();
  assert.equal(elements['agent-card'].hidden, true);
  // Wake the demo loops created at startup. They must not override live poses.
  timers.splice(0, 9).forEach(timer=>timer.fn());
  await Promise.resolve(); await Promise.resolve();
  assert.equal(run('agents.clarice.spr.children[1].src'), 'Clarice_01_working_48x48.png');
  for (const mutation of [
    data=>data.updatedAt=new Date(Date.now()-21000).toISOString(),
    data=>data.updatedAt=new Date(Date.now()+60000).toISOString(),
    data=>delete data.agents.iris,
    data=>data.agents.clarice.state='invalid',
    data=>data.agents.clarice.lastSeen='invalid',
    data=>data.agents.clarice.lastSeen=null,
    data=>data.agents.clarice.detail={text:'untrusted'},
  ]){
    sandbox.bad = structuredClone(sandbox.payload);
    mutation(sandbox.bad);
    assert.throws(()=>run('validateStatus(bad)'));
  }
  run('changeMode(false)');
  assert.equal(run('agents.aurelia.spr.dataset.state'), undefined);
  assert.equal(run('agents.clarice.stateLabel.textContent'), 'デモ');
  run('agents.clarice.walkTo(400)');
  run('applyStatus(payload)');
  frames.splice(0).forEach(fn=>fn(100));
  assert.equal(run('agents.clarice.pos.x'), run('agents.clarice.home.x'));
  assert.equal(run('agents.clarice.spr.children[1].src'), 'Clarice_01_working_48x48.png');
  sandbox.fetch = async()=>({ok:true,json:async()=>{throw new Error('invalid JSON');}});
  await run('pollStatus()');
  assert.equal(run('liveMode'), false);
  assert.match(elements.connection.textContent, /status.jsonを読み取れません/);
  sandbox.fetch = async()=>({ok:true,json:async()=>sandbox.payload});
  await run('pollStatus()');
  assert.equal(run('liveMode'), true);
  sandbox.fetch = async()=>({ok:false,status:404});
  await run('pollStatus()');
  assert.equal(run('liveMode'), false);
  assert.match(elements.connection.textContent, /status.jsonが見つかりません/);
  sandbox.stale = structuredClone(sandbox.payload);
  sandbox.stale.updatedAt = new Date(Date.now()-60000).toISOString();
  sandbox.fetch = async()=>({ok:true,json:async()=>sandbox.stale});
  await run('pollStatus()');
  assert.match(elements.connection.textContent, /見回り役の更新が停止/);
  assert.match(elements.connection.textContent, /最終更新/);
  console.log('PASS: state rendering, validation, missing/broken/stale status, recovery, demo cancellation');
}
module.exports = main();
