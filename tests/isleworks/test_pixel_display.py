"""Verify group counts and selection in the pixel observer without a browser."""
import shutil
import subprocess
import unittest
from pathlib import Path

@unittest.skipUnless(shutil.which('node'), 'Node.js is required')
class PixelDisplayTest(unittest.TestCase):
    def test_total_counts_sample_and_context_selection(self):
        script=r'''
const fs=require('fs'),vm=require('vm');
class E { constructor(){this.children=[];this._text='';this.style={};this.handlers={};this.width=800;this.height=490;}
set textContent(v){this._text=String(v);this.children=[]}get textContent(){return this._text+this.children.map(c=>c.textContent).join('')}
append(...v){this.children.push(...v)}addEventListener(n,f){this.handlers[n]=f}
getContext(){return {fillRect(){},fillText(){},strokeRect(){}}}
getBoundingClientRect(){return {left:0,top:0,width:800,height:490}} }
const ids=['pixel-scene','pixel-snapshot-label','pixel-counts','pixel-groups','pixel-selected']; const els=Object.fromEntries(ids.map(x=>[x,new E()]));
const document={getElementById:id=>els[id],createElement:()=>new E(),createTextNode:s=>({textContent:s})};
const window={};vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{document,window,requestAnimationFrame:()=>1,Math});
let selected=null;
const resident={id:'r1',name:'Resident 1',active:true};
const detail={run:{id:'one'},state:{day:1,residents:{r1:resident}},plans:{r1:{schedule:[{day:1,labor:['fish','gather','gather','fish']}]}},population:{total:10000,planned:10000,counts:{fish:8000,gather:2000,craft:0,rest:0,other:0}}};
window.IsleworksPixel.render(detail,id=>selected=id);
if(!els['pixel-groups'].textContent.includes('捕鱼 8000')||!els['pixel-counts'].textContent.includes('10000'))throw Error('not full-population counts');
els['pixel-scene'].handlers.click({clientX:115,clientY:330});
if(selected!=='r1')throw Error('pixel selection did not reach resident details');
window.IsleworksPixel.render({...detail,run:{id:'two'}},()=>{});
if(!els['pixel-selected'].textContent.includes('点击画面'))throw Error('selection leaked into another run');
'''
        root=Path(__file__).resolve().parents[2]
        result=subprocess.run(['node','-e',script,str(root/'src/isleworks/interfaces/web/pixel-scene.js')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
