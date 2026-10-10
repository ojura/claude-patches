'use strict';
// Component adapter: exact pinned graph helpers, independent PE offset reading.
// Produces a Bun blob only. PE insertion is performed by the Python writer.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const ts=require(process.env.TYPESCRIPT_PATH||'typescript');
const [branch,input,output,mode='literal']=process.argv.slice(2);
if(!['main','pr'].includes(branch)||!input||!output)throw Error('branch input output mode required');
const file=path.join(__dirname,branch==='main'?'tweakcc-main-nativeInstallation.ts':'tweakcc-pr987-nativeInstallation.ts');
let source=fs.readFileSync(file,'utf8');
source+='\nexport {parseBunDataBlob,mapModules,buildSectionData,isClaudeModule'+(branch==='main'?',rebuildBunData':'')+'};\n';
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
const m={exports:{}};
function req(id){
 if(id==='node-lief')return {__esModule:true,default:new Proxy({}, {get(){throw Error('node-lief path was not authorized for component test');}})};
 if(id==='./utils')return {debug:()=>{},isDebug:()=>false};
 if(id.startsWith('node:'))return require(id);
 throw Error('unexpected dependency '+id);
}
new Function('require','module','exports',code)(req,m,m.exports);const api=m.exports;
const b=fs.readFileSync(input),po=b.readUInt32LE(60),opt=po+24,n=b.readUInt16LE(po+6),so=opt+b.readUInt16LE(po+20);
let bs;for(let i=0;i<n;i++){let at=so+i*40;if(b.subarray(at,at+8).toString().replace(/\0.*$/s,'')==='.bun')bs={offset:b.readUInt32LE(at+20)};}
if(!bs)throw Error('missing .bun');
const length=Number(b.readBigUInt64LE(bs.offset));let blob=b.subarray(bs.offset+8,bs.offset+8+length);
const trailer=Buffer.from('\n---- Bun! ----\n');
if(!blob.subarray(-trailer.length).equals(trailer)&&blob.subarray(-trailer.length-1,-1).equals(trailer))blob=blob.subarray(0,-1);
const parsed=api.parseBunDataBlob(blob),mods=[];
api.mapModules(blob,parsed.bunOffsets,parsed.moduleStructSize,(record,name,index)=>{
 mods.push({record,name,index,contents:blob.subarray(record.contents.offset,record.contents.offset+record.contents.length)});
});
const selected=mods.find(x=>api.isClaudeModule(x.name));if(!selected)throw Error('no source target');
let old=selected.contents.toString('utf8'),next=old;
if(mode==='literal')next=old.replaceAll('(Claude Code)','(ClauDe Code)');
else if(mode==='repeat')next=old.replaceAll('(ClauDe Code)','(CLAUDE Code)');
else if(mode==='unicode')next=old.replaceAll('(Claude Code)','(Claude 🦆)');
else if(mode==='shrink')next=old.replaceAll('(Claude Code)','(CC)');
else if(mode==='dependency')next='import "B:/~BUN/root/recovery-missing.js";\n'+old;
else if(mode!=='noop')throw Error('bad mode');
if(next===old&&mode!=='noop')throw Error('no edit anchor');
let result=branch==='main'?api.rebuildBunData(blob,parsed.bunOffsets,Buffer.from(next),parsed.moduleStructSize):api.replaceBunModuleSources(blob,[{index:selected.index,name:selected.name,contents:Buffer.from(next)}]);
fs.writeFileSync(output,api.buildSectionData(result,8),{flag:'wx'});
console.log(JSON.stringify({branch,mode,selected:selected.index,sourceFileHash:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'),inputGraph:blob.length,outputGraph:result.length,sourceBefore:Buffer.byteLength(old),sourceAfter:Buffer.byteLength(next)}));
