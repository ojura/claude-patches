'use strict';
// Test adapter only. Executes pinned CURRENT-MAIN rebuild/ELF helpers without
// node-lief. This file is not imported or required by the Python handler.
const fs = require('node:fs'), path=require('node:path');
const ts=require(process.env.TYPESCRIPT_PATH || 'typescript');
const sourcePath=path.join(__dirname,'tweakcc-main-nativeInstallation.ts');
const source=fs.readFileSync(sourcePath,'utf8')+'\nexport {parseBunDataBlob,mapModules,buildSectionData,rebuildBunData,isClaudeModule,extractBunDataFromSection};\n';
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
const m={exports:{}};
const requirePinned=id=>{
 if(id==='node-lief')return {__esModule:true,default:new Proxy({}, {get(){throw Error('Unexpected node-lief call');}})};
 if(id==='./utils')return {debug:()=>{},isDebug:()=>false};
 if(['node:fs','node:child_process','node:crypto','node:buffer'].includes(id))return require(id);
 throw Error('Unexpected dependency '+id);
};
new Function('require','module','exports',js)(requirePinned,m,m.exports);
const api=m.exports;
const [input,output,mode='literal']=process.argv.slice(2);
if(!input||!output)throw Error('usage: node make_tweakcc_main_binary.cjs input output [literal|unicode|shrink|repeat|noop|dependency]');
const data=fs.readFileSync(input),u16=o=>data.readUInt16LE(o),u32=o=>data.readUInt32LE(o),u64=o=>Number(data.readBigUInt64LE(o));
const shoff=u64(40),shsize=u16(58),shcount=u16(60),ns=shoff+u16(62)*shsize,names=data.subarray(u64(ns+24),u64(ns+24)+u64(ns+32));
let sec;
for(let i=0;i<shcount;i++){
 let o=shoff+i*shsize,n=u32(o),end=names.indexOf(0,n);
 if(names.subarray(n,end).toString()==='.bun')sec={offset:u64(o+24),size:u64(o+32),address:u64(o+16)};
}
if(!sec)throw Error('No .bun');
const phoff=u64(32),phsize=u16(54),phcount=u16(56);let seg;
for(let i=0;i<phcount;i++){
 let o=phoff+i*phsize;
 if(u32(o)===1 && u64(o+8)<=sec.offset && u64(o+8)+u64(o+32)>=sec.offset+sec.size)
  seg={offset:u64(o+8),address:u64(o+16),size:u64(o+32),memory:u64(o+40)};
}
if(!seg)throw Error('No containing load');
let blob=data.subarray(sec.offset+8,sec.offset+sec.size);
const trailer=Buffer.from('\n---- Bun! ----\n');
if(!blob.subarray(-trailer.length).equals(trailer)&&blob.subarray(-trailer.length-1,-1).equals(trailer))blob=blob.subarray(0,-1);
const p=api.parseBunDataBlob(blob),mods=[];
api.mapModules(blob,p.bunOffsets,p.moduleStructSize,(record,name,index)=>{mods.push({record,name,index,contents:blob.subarray(record.contents.offset,record.contents.offset+record.contents.length)});});
const selected=mods.find(m=>api.isClaudeModule(m.name));
if(!selected)throw Error('No entrypoint name match');
const old=selected.contents.toString('utf8'); let next=old;
if(mode==='literal')next=old.replaceAll('(Claude Code)','(ClauDe Code)');
else if(mode==='repeat')next=old.replaceAll('(ClauDe Code)','(CLAUDE Code)');
else if(mode==='unicode')next=old.replaceAll('(Claude Code)','(Claude 🦆)');
else if(mode==='shrink')next=old.replaceAll('(Claude Code)','(CC)');
else if(mode==='noop')next=old;
else if(mode==='dependency'){
 if(selected.record.moduleFormat===1)next='import "/$bunfs/root/recovery-missing.js";\n'+old;
 else next=old.replace('(function(exports, require, module, __filename, __dirname) {',
 '(function(exports, require, module, __filename, __dirname) {require("/$bunfs/root/recovery-missing.js");');
}
if(next===old && mode!=='noop')throw Error('No replacement anchor');
let sectionExtraction;
try {const parsed=api.extractBunDataFromSection(data.subarray(sec.offset,sec.offset+sec.size));sectionExtraction={ok:true};}
catch(e){sectionExtraction={ok:false,error:e.message};}
const updated=api.rebuildBunData(blob,p.bunOffsets,Buffer.from(next,'utf8'),p.moduleStructSize);
const wrapped=api.buildSectionData(updated,8);
const out=api.replaceTailBunSection({file:data,bunFileOffset:BigInt(sec.offset),bunVirtualAddress:BigInt(sec.address),bunSize:BigInt(sec.size),rwFileOffset:BigInt(seg.offset),rwVirtualAddress:BigInt(seg.address),rwFileSize:BigInt(seg.size),rwVirtualSize:BigInt(seg.memory),pageSize:4096n,newSectionData:wrapped});
if(!out)throw Error('Pinned helper declined input layout');
fs.writeFileSync(output,out,{mode:0o700,flag:'wx'});
console.log(JSON.stringify({input,output,mode,selected:selected.index,sectionExtraction,normalizedInputPad:blob.length !== sec.size-8,inputBytes:data.length,outputBytes:out.length}));
