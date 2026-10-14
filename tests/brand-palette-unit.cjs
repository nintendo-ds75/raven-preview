// Static palette/contrast checks; these do not replace browser visual QA.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const css=fs.readFileSync(path.join(__dirname,'../web/style.css'),'utf8');
const expected={bg:'#0d0d0d',card:'#151614',ink:'#f5f5f1',green:'#83d97a',line:'#2b2e27',muted:'#92968d',text:'#bebfb6',amber:'#e4ba6a'};
for(const [name,value] of Object.entries(expected)) assert.ok(css.includes(`--${name}:${value}`),`Brand token ${name}`);
function luminance(hex){const c=hex.slice(1).match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return .2126*c[0]+.7152*c[1]+.0722*c[2];}
function contrast(a,b){const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);}
for(const foreground of [expected.ink,expected.text,expected.muted,expected.green,expected.amber]) for(const background of [expected.bg,expected.card]) assert.ok(contrast(foreground,background)>=4.5,`${foreground} on ${background}`);
assert.ok(contrast('#10110f',expected.green)>=4.5,'Primary action text');
assert.match(css,/summary:focus-visible/);
assert.match(css,/\.brand img,\.work-home>img\{filter:none\}/, 'Preserve multicolor SVG artwork rather than flattening it into a white square');
console.log('Verified brand tokens and 11 text contrast pairs pass (static CSS, not browser screenshots).');
