import{d as V,D as r,av as oe,p as W,aY as ae,aZ as le,a_ as ne,a$ as ce,b0 as fe,X,Z as p,$ as q,J as he,a4 as ue,a6 as ve,b1 as me,aN as Z,x as ye,q as be,aw as Y,N as xe,c as P,e as f,a as x,w as z,l as h,F as J,i as K,h as T,y as ke,r as N,o as C,ag as F,s as j,t as M,A as Q,ax as ee,aS as $e,ay as te,_ as we}from"./index-BRWE7ryn.js";import{N as re}from"./Select-B5W3XdWI.js";import{f as A}from"./Popover-BGHQkDWN.js";import{u as Ce}from"./use-message-BwMoGsx0.js";import{N as G,a as ie}from"./Tabs-BMYII_DM.js";import{N as H}from"./Input-DrcD-Lz9.js";import{N as Se}from"./Switch-BcjF0E1f.js";import{N as _e}from"./Tag-CpH9AoX_.js";import{N as Pe}from"./Popconfirm-CuHH5Kes.js";import"./FocusDetector-CoWHO28J.js";import"./happens-in-CM8LO42l.js";import"./Add-BrqBoYHY.js";const Ne={success:r(ce,null),error:r(ne,null),warning:r(le,null),info:r(ae,null)},ze=V({name:"ProgressCircle",props:{clsPrefix:{type:String,required:!0},status:{type:String,required:!0},strokeWidth:{type:Number,required:!0},fillColor:[String,Object],railColor:String,railStyle:[String,Object],percentage:{type:Number,default:0},offsetDegree:{type:Number,default:0},showIndicator:{type:Boolean,required:!0},indicatorTextColor:String,unit:String,viewBoxWidth:{type:Number,required:!0},gapDegree:{type:Number,required:!0},gapOffsetDegree:{type:Number,default:0}},setup(e,{slots:u}){const m=W(()=>{const l="gradient",{fillColor:s}=e;return typeof s=="object"?`${l}-${fe(JSON.stringify(s))}`:l});function w(l,s,n,v){const{gapDegree:y,viewBoxWidth:b,strokeWidth:k}=e,d=50,$=0,c=d,o=0,S=2*d,B=50+k/2,_=`M ${B},${B} m ${$},${c}
      a ${d},${d} 0 1 1 ${o},${-S}
      a ${d},${d} 0 1 1 ${-o},${S}`,R=Math.PI*2*d,D={stroke:v==="rail"?n:typeof e.fillColor=="object"?`url(#${m.value})`:n,strokeDasharray:`${Math.min(l,100)/100*(R-y)}px ${b*8}px`,strokeDashoffset:`-${y/2}px`,transformOrigin:s?"center":void 0,transform:s?`rotate(${s}deg)`:void 0};return{pathString:_,pathStyle:D}}const g=()=>{const l=typeof e.fillColor=="object",s=l?e.fillColor.stops[0]:"",n=l?e.fillColor.stops[1]:"";return l&&r("defs",null,r("linearGradient",{id:m.value,x1:"0%",y1:"100%",x2:"100%",y2:"0%"},r("stop",{offset:"0%","stop-color":s}),r("stop",{offset:"100%","stop-color":n})))};return()=>{const{fillColor:l,railColor:s,strokeWidth:n,offsetDegree:v,status:y,percentage:b,showIndicator:k,indicatorTextColor:d,unit:$,gapOffsetDegree:c,clsPrefix:o}=e,{pathString:S,pathStyle:B}=w(100,0,s,"rail"),{pathString:_,pathStyle:R}=w(b,v,l,"fill"),D=100+n;return r("div",{class:`${o}-progress-content`,role:"none"},r("div",{class:`${o}-progress-graph`,"aria-hidden":!0},r("div",{class:`${o}-progress-graph-circle`,style:{transform:c?`rotate(${c}deg)`:void 0}},r("svg",{viewBox:`0 0 ${D} ${D}`},g(),r("g",null,r("path",{class:`${o}-progress-graph-circle-rail`,d:S,"stroke-width":n,"stroke-linecap":"round",fill:"none",style:B})),r("g",null,r("path",{class:[`${o}-progress-graph-circle-fill`,b===0&&`${o}-progress-graph-circle-fill--empty`],d:_,"stroke-width":n,"stroke-linecap":"round",fill:"none",style:R}))))),k?r("div",null,u.default?r("div",{class:`${o}-progress-custom-content`,role:"none"},u.default()):y!=="default"?r("div",{class:`${o}-progress-icon`,"aria-hidden":!0},r(oe,{clsPrefix:o},{default:()=>Ne[y]})):r("div",{class:`${o}-progress-text`,style:{color:d},role:"none"},r("span",{class:`${o}-progress-text__percentage`},b),r("span",{class:`${o}-progress-text__unit`},$))):null)}}}),Be={success:r(ce,null),error:r(ne,null),warning:r(le,null),info:r(ae,null)},Re=V({name:"ProgressLine",props:{clsPrefix:{type:String,required:!0},percentage:{type:Number,default:0},railColor:String,railStyle:[String,Object],fillColor:[String,Object],status:{type:String,required:!0},indicatorPlacement:{type:String,required:!0},indicatorTextColor:String,unit:{type:String,default:"%"},processing:{type:Boolean,required:!0},showIndicator:{type:Boolean,required:!0},height:[String,Number],railBorderRadius:[String,Number],fillBorderRadius:[String,Number]},setup(e,{slots:u}){const m=W(()=>A(e.height)),w=W(()=>{var s,n;return typeof e.fillColor=="object"?`linear-gradient(to right, ${(s=e.fillColor)===null||s===void 0?void 0:s.stops[0]} , ${(n=e.fillColor)===null||n===void 0?void 0:n.stops[1]})`:e.fillColor}),g=W(()=>e.railBorderRadius!==void 0?A(e.railBorderRadius):e.height!==void 0?A(e.height,{c:.5}):""),l=W(()=>e.fillBorderRadius!==void 0?A(e.fillBorderRadius):e.railBorderRadius!==void 0?A(e.railBorderRadius):e.height!==void 0?A(e.height,{c:.5}):"");return()=>{const{indicatorPlacement:s,railColor:n,railStyle:v,percentage:y,unit:b,indicatorTextColor:k,status:d,showIndicator:$,processing:c,clsPrefix:o}=e;return r("div",{class:`${o}-progress-content`,role:"none"},r("div",{class:`${o}-progress-graph`,"aria-hidden":!0},r("div",{class:[`${o}-progress-graph-line`,{[`${o}-progress-graph-line--indicator-${s}`]:!0}]},r("div",{class:`${o}-progress-graph-line-rail`,style:[{backgroundColor:n,height:m.value,borderRadius:g.value},v]},r("div",{class:[`${o}-progress-graph-line-fill`,c&&`${o}-progress-graph-line-fill--processing`],style:{maxWidth:`${e.percentage}%`,background:w.value,height:m.value,lineHeight:m.value,borderRadius:l.value}},s==="inside"?r("div",{class:`${o}-progress-graph-line-indicator`,style:{color:k}},u.default?u.default():`${y}${b}`):null)))),$&&s==="outside"?r("div",null,u.default?r("div",{class:`${o}-progress-custom-content`,style:{color:k},role:"none"},u.default()):d==="default"?r("div",{role:"none",class:`${o}-progress-icon ${o}-progress-icon--as-text`,style:{color:k}},y,b):r("div",{class:`${o}-progress-icon`,"aria-hidden":!0},r(oe,{clsPrefix:o},{default:()=>Be[d]}))):null)}}});function se(e,u,m=100){return`m ${m/2} ${m/2-e} a ${e} ${e} 0 1 1 0 ${2*e} a ${e} ${e} 0 1 1 0 -${2*e}`}const De=V({name:"ProgressMultipleCircle",props:{clsPrefix:{type:String,required:!0},viewBoxWidth:{type:Number,required:!0},percentage:{type:Array,default:[0]},strokeWidth:{type:Number,required:!0},circleGap:{type:Number,required:!0},showIndicator:{type:Boolean,required:!0},fillColor:{type:Array,default:()=>[]},railColor:{type:Array,default:()=>[]},railStyle:{type:Array,default:()=>[]}},setup(e,{slots:u}){const m=W(()=>e.percentage.map((l,s)=>`${Math.PI*l/100*(e.viewBoxWidth/2-e.strokeWidth/2*(1+2*s)-e.circleGap*s)*2}, ${e.viewBoxWidth*8}`)),w=(g,l)=>{const s=e.fillColor[l],n=typeof s=="object"?s.stops[0]:"",v=typeof s=="object"?s.stops[1]:"";return typeof e.fillColor[l]=="object"&&r("linearGradient",{id:`gradient-${l}`,x1:"100%",y1:"0%",x2:"0%",y2:"100%"},r("stop",{offset:"0%","stop-color":n}),r("stop",{offset:"100%","stop-color":v}))};return()=>{const{viewBoxWidth:g,strokeWidth:l,circleGap:s,showIndicator:n,fillColor:v,railColor:y,railStyle:b,percentage:k,clsPrefix:d}=e;return r("div",{class:`${d}-progress-content`,role:"none"},r("div",{class:`${d}-progress-graph`,"aria-hidden":!0},r("div",{class:`${d}-progress-graph-circle`},r("svg",{viewBox:`0 0 ${g} ${g}`},r("defs",null,k.map(($,c)=>w($,c))),k.map(($,c)=>r("g",{key:c},r("path",{class:`${d}-progress-graph-circle-rail`,d:se(g/2-l/2*(1+2*c)-s*c,l,g),"stroke-width":l,"stroke-linecap":"round",fill:"none",style:[{strokeDashoffset:0,stroke:y[c]},b[c]]}),r("path",{class:[`${d}-progress-graph-circle-fill`,$===0&&`${d}-progress-graph-circle-fill--empty`],d:se(g/2-l/2*(1+2*c)-s*c,l,g),"stroke-width":l,"stroke-linecap":"round",fill:"none",style:{strokeDasharray:m.value[c],strokeDashoffset:0,stroke:typeof v[c]=="object"?`url(#gradient-${c})`:v[c]}})))))),n&&u.default?r("div",null,r("div",{class:`${d}-progress-text`},u.default())):null)}}}),Ie=X([p("progress",{display:"inline-block"},[p("progress-icon",`
 color: var(--n-icon-color);
 transition: color .3s var(--n-bezier);
 `),q("line",`
 width: 100%;
 display: block;
 `,[p("progress-content",`
 display: flex;
 align-items: center;
 `,[p("progress-graph",{flex:1})]),p("progress-custom-content",{marginLeft:"14px"}),p("progress-icon",`
 width: 30px;
 padding-left: 14px;
 height: var(--n-icon-size-line);
 line-height: var(--n-icon-size-line);
 font-size: var(--n-icon-size-line);
 `,[q("as-text",`
 color: var(--n-text-color-line-outer);
 text-align: center;
 width: 40px;
 font-size: var(--n-font-size);
 padding-left: 4px;
 transition: color .3s var(--n-bezier);
 `)])]),q("circle, dashboard",{width:"120px"},[p("progress-custom-content",`
 position: absolute;
 left: 50%;
 top: 50%;
 transform: translateX(-50%) translateY(-50%);
 display: flex;
 align-items: center;
 justify-content: center;
 `),p("progress-text",`
 position: absolute;
 left: 50%;
 top: 50%;
 transform: translateX(-50%) translateY(-50%);
 display: flex;
 align-items: center;
 color: inherit;
 font-size: var(--n-font-size-circle);
 color: var(--n-text-color-circle);
 font-weight: var(--n-font-weight-circle);
 transition: color .3s var(--n-bezier);
 white-space: nowrap;
 `),p("progress-icon",`
 position: absolute;
 left: 50%;
 top: 50%;
 transform: translateX(-50%) translateY(-50%);
 display: flex;
 align-items: center;
 color: var(--n-icon-color);
 font-size: var(--n-icon-size-circle);
 `)]),q("multiple-circle",`
 width: 200px;
 color: inherit;
 `,[p("progress-text",`
 font-weight: var(--n-font-weight-circle);
 color: var(--n-text-color-circle);
 position: absolute;
 left: 50%;
 top: 50%;
 transform: translateX(-50%) translateY(-50%);
 display: flex;
 align-items: center;
 justify-content: center;
 transition: color .3s var(--n-bezier);
 `)]),p("progress-content",{position:"relative"}),p("progress-graph",{position:"relative"},[p("progress-graph-circle",[X("svg",{verticalAlign:"bottom"}),p("progress-graph-circle-fill",`
 stroke: var(--n-fill-color);
 transition:
 opacity .3s var(--n-bezier),
 stroke .3s var(--n-bezier),
 stroke-dasharray .3s var(--n-bezier);
 `,[q("empty",{opacity:0})]),p("progress-graph-circle-rail",`
 transition: stroke .3s var(--n-bezier);
 overflow: hidden;
 stroke: var(--n-rail-color);
 `)]),p("progress-graph-line",[q("indicator-inside",[p("progress-graph-line-rail",`
 height: 16px;
 line-height: 16px;
 border-radius: 10px;
 `,[p("progress-graph-line-fill",`
 height: inherit;
 border-radius: 10px;
 `),p("progress-graph-line-indicator",`
 background: #0000;
 white-space: nowrap;
 text-align: right;
 margin-left: 14px;
 margin-right: 14px;
 height: inherit;
 font-size: 12px;
 color: var(--n-text-color-line-inner);
 transition: color .3s var(--n-bezier);
 `)])]),q("indicator-inside-label",`
 height: 16px;
 display: flex;
 align-items: center;
 `,[p("progress-graph-line-rail",`
 flex: 1;
 transition: background-color .3s var(--n-bezier);
 `),p("progress-graph-line-indicator",`
 background: var(--n-fill-color);
 font-size: 12px;
 transform: translateZ(0);
 display: flex;
 vertical-align: middle;
 height: 16px;
 line-height: 16px;
 padding: 0 10px;
 border-radius: 10px;
 position: absolute;
 white-space: nowrap;
 color: var(--n-text-color-line-inner);
 transition:
 right .2s var(--n-bezier),
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier);
 `)]),p("progress-graph-line-rail",`
 position: relative;
 overflow: hidden;
 height: var(--n-rail-height);
 border-radius: 5px;
 background-color: var(--n-rail-color);
 transition: background-color .3s var(--n-bezier);
 `,[p("progress-graph-line-fill",`
 background: var(--n-fill-color);
 position: relative;
 border-radius: 5px;
 height: inherit;
 width: 100%;
 max-width: 0%;
 transition:
 background-color .3s var(--n-bezier),
 max-width .2s var(--n-bezier);
 `,[q("processing",[X("&::after",`
 content: "";
 background-image: var(--n-line-bg-processing);
 animation: progress-processing-animation 2s var(--n-bezier) infinite;
 `)])])])])])]),X("@keyframes progress-processing-animation",`
 0% {
 position: absolute;
 left: 0;
 top: 0;
 bottom: 0;
 right: 100%;
 opacity: 1;
 }
 66% {
 position: absolute;
 left: 0;
 top: 0;
 bottom: 0;
 right: 0;
 opacity: 0;
 }
 100% {
 position: absolute;
 left: 0;
 top: 0;
 bottom: 0;
 right: 0;
 opacity: 0;
 }
 `)]),We=Object.assign(Object.assign({},ue.props),{processing:Boolean,type:{type:String,default:"line"},gapDegree:Number,gapOffsetDegree:Number,status:{type:String,default:"default"},railColor:[String,Array],railStyle:[String,Array],color:[String,Array,Object],viewBoxWidth:{type:Number,default:100},strokeWidth:{type:Number,default:7},percentage:[Number,Array],unit:{type:String,default:"%"},showIndicator:{type:Boolean,default:!0},indicatorPosition:{type:String,default:"outside"},indicatorPlacement:{type:String,default:"outside"},indicatorTextColor:String,circleGap:{type:Number,default:1},height:Number,borderRadius:[String,Number],fillBorderRadius:[String,Number],offsetDegree:Number}),qe=V({name:"Progress",props:We,setup(e){const u=W(()=>e.indicatorPlacement||e.indicatorPosition),m=W(()=>{if(e.gapDegree||e.gapDegree===0)return e.gapDegree;if(e.type==="dashboard")return 75}),{mergedClsPrefixRef:w,inlineThemeDisabled:g}=he(e),l=ue("Progress","-progress",Ie,me,e,w),s=W(()=>{const{status:v}=e,{common:{cubicBezierEaseInOut:y},self:{fontSize:b,fontSizeCircle:k,railColor:d,railHeight:$,iconSizeCircle:c,iconSizeLine:o,textColorCircle:S,textColorLineInner:B,textColorLineOuter:_,lineBgProcessing:R,fontWeightCircle:D,[Z("iconColor",v)]:O,[Z("fillColor",v)]:I}}=l.value;return{"--n-bezier":y,"--n-fill-color":I,"--n-font-size":b,"--n-font-size-circle":k,"--n-font-weight-circle":D,"--n-icon-color":O,"--n-icon-size-circle":c,"--n-icon-size-line":o,"--n-line-bg-processing":R,"--n-rail-color":d,"--n-rail-height":$,"--n-text-color-circle":S,"--n-text-color-line-inner":B,"--n-text-color-line-outer":_}}),n=g?ve("progress",W(()=>e.status[0]),s,e):void 0;return{mergedClsPrefix:w,mergedIndicatorPlacement:u,gapDeg:m,cssVars:g?void 0:s,themeClass:n==null?void 0:n.themeClass,onRender:n==null?void 0:n.onRender}},render(){const{type:e,cssVars:u,indicatorTextColor:m,showIndicator:w,status:g,railColor:l,railStyle:s,color:n,percentage:v,viewBoxWidth:y,strokeWidth:b,mergedIndicatorPlacement:k,unit:d,borderRadius:$,fillBorderRadius:c,height:o,processing:S,circleGap:B,mergedClsPrefix:_,gapDeg:R,gapOffsetDegree:D,themeClass:O,$slots:I,onRender:U}=this;return U==null||U(),r("div",{class:[O,`${_}-progress`,`${_}-progress--${e}`,`${_}-progress--${g}`],style:u,"aria-valuemax":100,"aria-valuemin":0,"aria-valuenow":v,role:e==="circle"||e==="line"||e==="dashboard"?"progressbar":"none"},e==="circle"||e==="dashboard"?r(ze,{clsPrefix:_,status:g,showIndicator:w,indicatorTextColor:m,railColor:l,fillColor:n,railStyle:s,offsetDegree:this.offsetDegree,percentage:v,viewBoxWidth:y,strokeWidth:b,gapDegree:R===void 0?e==="dashboard"?75:0:R,gapOffsetDegree:D,unit:d},I):e==="line"?r(Re,{clsPrefix:_,status:g,showIndicator:w,indicatorTextColor:m,railColor:l,fillColor:n,railStyle:s,percentage:v,processing:S,indicatorPlacement:k,unit:d,fillBorderRadius:c,railBorderRadius:$,height:o},I):e==="multiple-circle"?r(De,{clsPrefix:_,strokeWidth:b,railColor:l,fillColor:n,railStyle:s,viewBoxWidth:y,percentage:v,showIndicator:w,circleGap:B},I):null)}}),Te={class:"media-view"},je={class:"panel-row"},Oe={class:"glass-panel panel main"},Me={class:"tts-controls"},Ae=["src"],Ge={class:"glass-panel panel side"},Ue={class:"cfg"},Ve={class:"glass-panel panel"},Le={class:"glass-panel panel"},Xe={class:"queue-hint"},Ye={class:"glass-panel section"},Fe={class:"task-list"},He={class:"task-kind"},Ee={class:"task-prompt"},Ze={key:1,class:"task-error"},Je=["href"],Ke={key:0,class:"empty-hint"},Qe={class:"glass-panel section"},et={class:"gallery-head"},tt={class:"gallery-grid"},rt=["src","onClick"],it=["src"],st=["src"],ot={class:"gallery-meta"},at={class:"g-name"},lt={key:0,class:"empty-hint"},nt=V({__name:"MediaView",setup(e){const u=Ce(),m=ye(),w=ke(),g=N(""),l=N("nahida"),s=N(null),n=N([]),v=N([]),y=N(""),b=N(!1),k=N(""),d=N(""),$=N(""),c=N([]),o=N("image"),S=N([]);be(async()=>{try{const a=await Y("/media/tts/voices");n.value=a.voices.map(i=>({label:`${i.id}${i.description?" · "+i.description.slice(0,16):""}`,value:i.id})),v.value=a.styles.map(i=>({label:i,value:i}));const t=await Y("/media/tts/config");m.autoSpeak=t.auto_speak,l.value=t.default_voice||"nahida"}catch{}I(),L(),w.on("media_task_update",B)}),xe(()=>w.off("media_task_update",B));function B(a){const t=c.value.find(i=>i.id===a.task_id);t?(t.status=a.status,t.progress=a.progress,a.result_url&&(t.result_path=a.result_url),a.error&&(t.error=a.error)):I(),a.status==="done"&&(u.success("生成完成 ✓"),L()),a.status==="failed"&&a.error&&u.error(`任务失败：${a.error}`)}async function _(){if(g.value.trim()){b.value=!0;try{const a=await ee("/media/tts",{text:g.value,voice:l.value,style:s.value||""});y.value=a.audio_url,a.cached&&u.info("缓存命中，秒回 ⚡"),L()}catch(a){u.error(a.message)}finally{b.value=!1}}}async function R(a){try{await m.setAutoSpeak(a),u.success(`自动朗读已${a?"开启":"关闭"} ✓`)}catch(t){u.error(t.message)}}async function D(a){l.value=a;try{await $e("/media/tts/config",{default_voice:a})}catch{}}async function O(a){const t=a==="image"?k.value:d.value;if(t.trim()){$.value=a;try{await ee(`/media/${a}`,{prompt:t}),u.success("任务已入队（进度实时推送）"),I()}catch(i){u.error(i.message)}finally{$.value=""}}}async function I(){try{c.value=await Y("/media/tasks?limit=20")}catch{}}async function U(a){try{await te(`/media/tasks/${a}`),u.success("已取消"),I()}catch(t){u.error(t.message)}}async function L(){try{S.value=await Y(`/media/gallery?type=${o.value}&limit=48`)}catch(a){u.error(a.message)}}async function de(a){try{await te(`/media/gallery/${o.value}/${a}`,!0),S.value=S.value.filter(t=>t.name!==a),u.success("已删除")}catch(t){u.error(t.message)}}function ge(a){window.open(a,"_blank")}const pe={queued:"default",running:"info",done:"success",failed:"error"};return(a,t)=>(C(),P("div",Te,[t[19]||(t[19]=f("h2",{class:"view-title"},"🎙 媒体工坊",-1)),x(h(ie),{type:"line",animated:""},{default:z(()=>[x(h(G),{name:"tts",tab:"语音合成"},{default:z(()=>[f("div",je,[f("div",Oe,[x(h(H),{value:g.value,"onUpdate:value":t[0]||(t[0]=i=>g.value=i),type:"textarea",rows:4,placeholder:"输入要合成的文本（≤500 字）…",maxlength:"500","show-count":""},null,8,["value"]),f("div",Me,[x(h(re),{value:l.value,"onUpdate:value":[t[1]||(t[1]=i=>l.value=i),D],options:n.value,placeholder:"音色",style:{"max-width":"220px"}},null,8,["value","options"]),x(h(re),{value:s.value,"onUpdate:value":t[2]||(t[2]=i=>s.value=i),options:v.value,placeholder:"情绪风格（自动）",clearable:"",style:{"max-width":"180px"}},null,8,["value","options"]),x(h(F),{type:"primary",loading:b.value,onClick:_},{default:z(()=>[...t[8]||(t[8]=[j("🎵 合成",-1)])]),_:1},8,["loading"])]),y.value?(C(),P("audio",{key:0,src:y.value,controls:"",autoplay:"",class:"tts-player"},null,8,Ae)):T("",!0)]),f("div",Ge,[t[10]||(t[10]=f("h4",null,"朗读设置",-1)),f("label",Ue,[t[9]||(t[9]=j(" 自动朗读回复 ",-1)),x(h(Se),{value:h(m).autoSpeak,"onUpdate:value":R},null,8,["value"])]),t[11]||(t[11]=f("p",{class:"cfg-hint"},"开启后，聊天页收到回复会自动合成并播放（音色跟随当前 Agent 的 voice_ref）。",-1))])])]),_:1}),x(h(G),{name:"image",tab:"图片生成"},{default:z(()=>[f("div",Ve,[x(h(H),{value:k.value,"onUpdate:value":t[3]||(t[3]=i=>k.value=i),type:"textarea",rows:3,placeholder:"描述想生成的画面…"},null,8,["value"]),x(h(F),{type:"primary",style:{"margin-top":"10px"},loading:$.value==="image",onClick:t[4]||(t[4]=i=>O("image"))},{default:z(()=>[...t[12]||(t[12]=[j(" 🎨 提交生成任务 ",-1)])]),_:1},8,["loading"])])]),_:1}),x(h(G),{name:"video",tab:"视频生成"},{default:z(()=>[f("div",Le,[f("p",Xe,"⏳ 视频生成耗时较长（数分钟），队列串行执行，进度实时推送。 当前队列 "+M(c.value.filter(i=>i.status==="queued"||i.status==="running").length)+" 个任务。",1),x(h(H),{value:d.value,"onUpdate:value":t[5]||(t[5]=i=>d.value=i),type:"textarea",rows:3,placeholder:"描述想生成的视频…"},null,8,["value"]),x(h(F),{type:"primary",style:{"margin-top":"10px"},loading:$.value==="video",onClick:t[6]||(t[6]=i=>O("video"))},{default:z(()=>[...t[13]||(t[13]=[j(" 🎬 提交生成任务 ",-1)])]),_:1},8,["loading"])])]),_:1})]),_:1}),f("section",Ye,[t[15]||(t[15]=f("h3",null,"任务队列",-1)),f("div",Fe,[(C(!0),P(J,null,K(c.value,i=>(C(),P("div",{key:i.id,class:"task-row"},[x(h(_e),{size:"small",type:pe[i.status],bordered:!1},{default:z(()=>[j(M(i.status),1)]),_:2},1032,["type"]),f("span",He,M(i.kind),1),f("span",Ee,M(i.prompt),1),i.status==="running"?(C(),Q(h(qe),{key:0,type:"line",percentage:Math.round((i.progress||0)*100),style:{"max-width":"140px"},height:6},null,8,["percentage"])):T("",!0),i.error?(C(),P("span",Ze,M(i.error),1)):T("",!0),i.result_path&&i.status==="done"?(C(),P("a",{key:2,href:i.result_path,target:"_blank",class:"task-link"},"查看",8,Je)):T("",!0),i.status==="queued"?(C(),Q(h(F),{key:3,size:"tiny",quaternary:"",onClick:E=>U(i.id)},{default:z(()=>[...t[14]||(t[14]=[j("取消",-1)])]),_:1},8,["onClick"])):T("",!0)]))),128)),c.value.length?T("",!0):(C(),P("div",Ke,"（暂无任务）"))])]),f("section",Qe,[f("div",et,[t[16]||(t[16]=f("h3",null,"画廊",-1)),x(h(ie),{type:"segment",size:"small",value:o.value,"onUpdate:value":[t[7]||(t[7]=i=>o.value=i),L],style:{"max-width":"280px"}},{default:z(()=>[x(h(G),{name:"image",tab:"图片"}),x(h(G),{name:"video",tab:"视频"}),x(h(G),{name:"audio",tab:"音频"})]),_:1},8,["value"])]),f("div",tt,[(C(!0),P(J,null,K(S.value,i=>(C(),P("div",{key:i.name,class:"gallery-card"},[o.value==="image"?(C(),P("img",{key:0,src:i.url,loading:"lazy",onClick:E=>ge(i.url)},null,8,rt)):o.value==="video"?(C(),P("video",{key:1,src:i.url,controls:"",preload:"metadata"},null,8,it)):(C(),P("audio",{key:2,src:i.url,controls:""},null,8,st)),f("div",ot,[f("span",at,M(i.name),1),x(h(Pe),{onPositiveClick:E=>de(i.name)},{trigger:z(()=>[...t[17]||(t[17]=[f("button",{class:"g-del"},"🗑",-1)])]),default:z(()=>[t[18]||(t[18]=j(" 确认删除该文件？ ",-1))]),_:1},8,["onPositiveClick"])])]))),128)),S.value.length?T("",!0):(C(),P("div",lt,"这里还没有长出叶子哦～生成点什么吧"))])])]))}}),kt=we(nt,[["__scopeId","data-v-7365599b"]]);export{kt as default};
