const {contextBridge,ipcRenderer}=require('electron');
const invoke=name=>(...args)=>ipcRenderer.invoke(`gumi:${name}`,...args);
const subscribe=name=>callback=>{const listener=(_event,value)=>callback(value);ipcRenderer.on(`gumi:${name}`,listener);return()=>ipcRenderer.removeListener(`gumi:${name}`,listener);};
contextBridge.exposeInMainWorld('gumi',{
 preferences:invoke('preferences'),savePreferences:invoke('savePreferences'),importWorkbench:invoke('importWorkbench'),open:invoke('open'),
 apiSettings:invoke('apiSettings'),apiSettingsSave:invoke('apiSettingsSave'),apiSettingsTest:invoke('apiSettingsTest'),onApiSettingsChanged:subscribe('apiSettingsChanged'),
 gumCodeInstructions:invoke('gumCodeInstructions'),gumCodeSaveInstructions:invoke('gumCodeSaveInstructions'),gumCodePickAttachments:invoke('gumCodePickAttachments'),gumCodeMemoryState:invoke('gumCodeMemoryState'),gumCodeMemoryAction:invoke('gumCodeMemoryAction'),gumCodeSkills:invoke('gumCodeSkills'),gumCodeModels:invoke('gumCodeModels'),gumCodeState:invoke('gumCodeState'),gumCodeNew:invoke('gumCodeNew'),gumCodePickWorkspace:invoke('gumCodePickWorkspace'),gumCodeSelect:invoke('gumCodeSelect'),gumCodeRename:invoke('gumCodeRename'),gumCodeSend:invoke('gumCodeSend'),gumCodeApprove:invoke('gumCodeApprove'),gumCodeStop:invoke('gumCodeStop'),onGumCode:subscribe('gumCode')
});
