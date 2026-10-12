const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {pathToFileURL} = require('node:url');
const {isCredentialPath} = require('./domain.cjs');
function authorizedSender(event,window,entryFile){return !window.isDestroyed() && event.sender===window.webContents && event.senderFrame===event.sender.mainFrame && event.senderFrame?.url===pathToFileURL(entryFile).href;}
function createPreferences(userData){
 const file=path.join(userData,'preferences.json');let state={settings:{language:'zh'},directories:[]};
 fs.mkdirSync(userData,{recursive:true,mode:0o700});
 if(fs.lstatSync(userData).isSymbolicLink())throw Error('应用目录无效。');
 if(fs.existsSync(file)){try{if(fs.lstatSync(file).isSymbolicLink()||fs.statSync(file).size>65536)throw Error();state=JSON.parse(fs.readFileSync(file,'utf8'));if(!['zh','en'].includes(state.settings?.language)||!Array.isArray(state.directories)||state.directories.length>200||!state.directories.every(p=>typeof p==='string'&&path.isAbsolute(p)&&!isCredentialPath(p)))throw Error();}catch{throw Error('本机设置无法读取，原文件已保留。');}}
 const save=next=>{const temp=path.join(userData,`.preferences-${randomUUID()}.tmp`);try{fs.writeFileSync(temp,JSON.stringify(next),{flag:'wx',mode:0o600});fs.renameSync(temp,file);state=next;}finally{try{fs.unlinkSync(temp);}catch{}}};
 return {state:()=>({settings:{...state.settings}}),save(request){if(!request||Object.keys(request).length!==1||!request.settings||Object.keys(request.settings).length!==1||!['zh','en'].includes(request.settings.language))throw Error('语言设置无效。');save({...state,settings:request.settings});return this.state();},allowed(target){try{return !isCredentialPath(target)&&state.directories.includes(fs.realpathSync(target));}catch{return false;}},grant(target){if(typeof target!=='string'||!path.isAbsolute(target)||isCredentialPath(target))throw Error('工作区路径无效。');const canonical=fs.realpathSync(target);if(!fs.statSync(canonical).isDirectory()||isCredentialPath(canonical))throw Error('工作区不可用。');if(!state.directories.includes(canonical)){if(state.directories.length>=200)throw Error('工作区数量已满。');save({...state,directories:[...state.directories,canonical]});}return canonical;}};
}
module.exports={createPreferences,authorizedSender};
