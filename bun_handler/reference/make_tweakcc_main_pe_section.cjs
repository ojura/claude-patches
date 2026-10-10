'use strict';
// Test adapter only. Executes the pinned CURRENT-MAIN Bun graph rebuild and
// section-wrapper helpers against a PE .bun payload. node-lief is a throwing
// sentinel; this deliberately does NOT test tweakcc's LIEF PE file writer.
const fs=require('node:fs'), path=require('node:path');
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
if(!input||!output)throw Error('usage: node make_tweakcc_main_pe_section.cjs input output [literal|unicode|shrink|noop|dependency]');
const data=fs.readFileSync(input);
const peoff=data.readUInt32LE(0x3c);
if(data.subarray(peoff,peoff+4).toString('binary')!=='PE\0\0')throw Error('not PE');
const coff=peoff+4,nsects=data.readUInt16LE(coff+2),optsz=data.readUInt16LE(coff+16),sh=coff+20+optsz;
let sec;
for(let i=0;i<nsects;i++){
 const o=sh+i*40,name=data.subarray(o,o+8).subarray(0,data.subarray(o,o+8).indexOf(0)>=0?data.subarray(o,o+8).indexOf(0):8).toString('ascii');
 if(name==='.bun'){
   if(sec)throw Error('multiple .bun');
   sec={index:i,header:o,virtualSize:data.readUInt32LE(o+8),virtualAddress:data.readUInt32LE(o+12),rawSize:data.readUInt32LE(o+16),rawOffset:data.readUInt32LE(o+20)};
 }
}
if(!sec)throw Error('no .bun');
const raw=data.subarray(sec.rawOffset,sec.rawOffset+sec.rawSize);
const parsedSection=api.extractBunDataFromSection(raw);
const blob=parsedSection.bunData,p=api.parseBunDataBlob(blob),mods=[];
api.mapModules(blob,p.bunOffsets,p.moduleStructSize,(record,name,index)=>{mods.push({record,name,index,contents:blob.subarray(record.contents.offset,record.contents.offset+record.contents.length)});});
const selected=mods.find(x=>api.isClaudeModule(x.name));
if(!selected)throw Error('no current-main selected module');
const old=selected.contents.toString('utf8'); let next=old;
if(mode==='literal')next=old.replaceAll('(Claude Code)','(ClauDe Code)');
else if(mode==='unicode')next=old.replaceAll('(Claude Code)','(Claude 🦆)');
else if(mode==='shrink')next=old.replaceAll('(Claude Code)','(CC)');
else if(mode==='noop')next=old;
else if(mode==='dependency'){
 const missing='B:/~BUN/root/recovery-missing.js';
 if(selected.record.moduleFormat===1)next='import "'+missing+'";\n'+old;
 else next=old.replace('(function(exports, require, module, __filename, __dirname) {',
  '(function(exports, require, module, __filename, __dirname) {require("'+missing+'");');
}
if(next===old && mode!=='noop')throw Error('no replacement anchor');
const rebuilt=api.rebuildBunData(blob,p.bunOffsets,Buffer.from(next,'utf8'),p.moduleStructSize);
const wrapped=api.buildSectionData(rebuilt,parsedSection.sectionHeaderSize);
fs.writeFileSync(output,wrapped,{flag:'wx'});
console.log(JSON.stringify({mode,selected:selected.index,selectedName:selected.name,inputBytes:data.length,rawSectionBytes:raw.length,virtualSectionBytes:sec.virtualSize,sectionHeaderSize:parsedSection.sectionHeaderSize,rebuiltBlobBytes:rebuilt.length,wrappedBytes:wrapped.length}));
