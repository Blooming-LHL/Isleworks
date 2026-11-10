"""Render model/fact states without browser or provider requests."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for the JS rendering check')
class ReviewDisplayTest(unittest.TestCase):
    def test_final_and_historical_reports_and_unavailable_states(self):
        script = r'''
const fs=require('fs'), vm=require('vm');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this._text='';this.className='';this.id='';}
  set textContent(value){this._text=String(value);this.children=[];} get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
  append(...items){this.children.push(...items.map(x=>typeof x==='string'?{textContent:x}:x));}
  querySelector(selector){return this.children.find(x=>x.className==='review-item') || null;}
}
const root=new Element('div');
const document={getElementById:id=>id==='review'?root:null,createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text})};
const sandbox={document,window:{},URLSearchParams}; vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const render=sandbox.window.IsleworksReview.render;
const base={run:{id:'r1',status:'completed',config:{initial_fish_units:2000}},state:{day:2},metrics:{metrics:{final_food:1000}},facts_report:{metrics:{final_food:394000}},llm_review:{status:'schema_validated',summary:'FINAL SECRET',claims:[{metric_id:'final_food',value:394000,interpretation:'test',evidence_ids:['e1']}],proposal:{initial_fish_units_delta:250,hypothesis:'H',risk:'R'},eligible_for_screening:true}};
render(base,false);
if(!root.textContent.includes('394 鱼')||!root.textContent.includes('2 鱼 → 2.25 鱼')||!root.textContent.includes('尚未采纳')||!root.textContent.includes('e1'))throw Error('final facts/proposal missing: '+root.textContent);
render({...base,facts_report:null},true);
if(root.textContent.includes('FINAL SECRET')||!root.textContent.includes('回看历史')||!root.textContent.includes('1 鱼'))throw Error('historical leak');
render({...base,llm_review:{status:'failed',error_detail:{message:'fixture failure'}}},false);
if(!root.textContent.includes('fixture failure')||!root.textContent.includes('394 鱼'))throw Error('failure hides facts');
render({...base,llm_review:{status:'no_change'}},false);
if(!root.textContent.includes('未提出参数调整建议'))throw Error('missing no change');
'''
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(['node', '-e', script, str(root / 'src/isleworks/interfaces/web/review.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
