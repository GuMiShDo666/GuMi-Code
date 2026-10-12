import type { GumCodeBridge } from './gumCodeTypes';
export type Project = {id:string; name:string; rootPath?:string; archived?:boolean};
export type State = {settings:{language:'zh'|'en'}};
export type Commit = (change:(state:State)=>void)=>Promise<void>;
export type ApiSettingsState = { baseUrl: string; model: string; hasKey: boolean; revision: string };
export type ApiSettingsRequest = { baseUrl: string; model: string; apiKey?: string; revision: string };
export type ApiSettingsResult = { settings: ApiSettingsState } | { error: string };
export type MigrationResult = {imported?:boolean; alreadyImported?:boolean; sessions?:number; workspaces?:number; memories?:number; providers?:number; canceled?:boolean; error?:string};
export type Bridge = GumCodeBridge & {
 apiSettings:()=>Promise<ApiSettingsResult>;
 apiSettingsSave:(request:ApiSettingsRequest)=>Promise<ApiSettingsResult>;
 apiSettingsTest:(request:ApiSettingsRequest)=>Promise<{ok:true}|{error:string}>;
 onApiSettingsChanged:(callback:()=>void)=>()=>void;
 open:(request:{kind:'url';target:string})=>Promise<void>;
 preferences:()=>Promise<State>;
 savePreferences:(state:State)=>Promise<State>;
 importWorkbench:()=>Promise<MigrationResult>;
};
declare global {interface Window {gumi?:Bridge}}
export function bridge():Bridge {if(!window.gumi) throw Error('桌面接口不可用。'); return window.gumi;}
