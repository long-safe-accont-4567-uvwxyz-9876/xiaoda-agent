import{aU as oe,bA as te,bB as _,I as re,J as E,aG as P,r as k,au as D,a9 as w,ao as ne,ab as $,Z as T,a0 as C,$ as S,X as V,am as A,d as ae,bb as ie,D as G,a4 as j,L as de,a6 as se,p as H,aN as U,R as le}from"./index-BRWE7ryn.js";import{u as M}from"./Popover-BGHQkDWN.js";import{g as ue}from"./get-slot-Bk_rJcZu.js";function ce(e){const{borderColor:o,primaryColor:t,baseColor:i,textColorDisabled:d,inputColorDisabled:h,textColor2:s,opacityDisabled:l,borderRadius:u,fontSizeSmall:v,fontSizeMedium:g,fontSizeLarge:p,heightSmall:c,heightMedium:m,heightLarge:b,lineHeight:x}=e;return Object.assign(Object.assign({},te),{labelLineHeight:x,buttonHeightSmall:c,buttonHeightMedium:m,buttonHeightLarge:b,fontSizeSmall:v,fontSizeMedium:g,fontSizeLarge:p,boxShadow:`inset 0 0 0 1px ${o}`,boxShadowActive:`inset 0 0 0 1px ${t}`,boxShadowFocus:`inset 0 0 0 1px ${t}, 0 0 0 2px ${_(t,{alpha:.2})}`,boxShadowHover:`inset 0 0 0 1px ${t}`,boxShadowDisabled:`inset 0 0 0 1px ${o}`,color:i,colorDisabled:h,colorActive:"#0000",textColor:s,textColorDisabled:d,dotColorActive:t,dotColorDisabled:o,buttonBorderColor:o,buttonBorderColorActive:t,buttonBorderColorHover:o,buttonColor:i,buttonColorActive:i,buttonTextColor:s,buttonTextColorActive:t,buttonTextColorHover:t,opacityDisabled:l,buttonBoxShadowFocus:`inset 0 0 0 1px ${t}, 0 0 0 2px ${_(t,{alpha:.3})}`,buttonBoxShadowHover:"inset 0 0 0 1px #0000",buttonBoxShadow:"inset 0 0 0 1px #0000",buttonBorderRadius:u})}const be={common:oe,self:ce},xe={name:String,value:{type:[String,Number,Boolean],default:"on"},checked:{type:Boolean,default:void 0},defaultChecked:Boolean,disabled:{type:Boolean,default:void 0},label:String,size:String,onUpdateChecked:[Function,Array],"onUpdate:checked":[Function,Array],checkedValue:{type:Boolean,default:void 0}},N=ne("n-radio-group");function Ce(e){const o=re(N,null),{mergedClsPrefixRef:t,mergedComponentPropsRef:i}=E(e),d=P(e,{mergedSize(r){var n,a;const{size:f}=e;if(f!==void 0)return f;if(o){const{mergedSizeRef:{value:I}}=o;if(I!==void 0)return I}if(r)return r.mergedSize.value;const F=(a=(n=i==null?void 0:i.value)===null||n===void 0?void 0:n.Radio)===null||a===void 0?void 0:a.size;return F||"medium"},mergedDisabled(r){return!!(e.disabled||o!=null&&o.disabledRef.value||r!=null&&r.disabled.value)}}),{mergedSizeRef:h,mergedDisabledRef:s}=d,l=k(null),u=k(null),v=k(e.defaultChecked),g=$(e,"checked"),p=M(g,v),c=D(()=>o?o.valueRef.value===e.value:p.value),m=D(()=>{const{name:r}=e;if(r!==void 0)return r;if(o)return o.nameRef.value}),b=k(!1);function x(){if(o){const{doUpdateValue:r}=o,{value:n}=e;w(r,n)}else{const{onUpdateChecked:r,"onUpdate:checked":n}=e,{nTriggerFormInput:a,nTriggerFormChange:f}=d;r&&w(r,!0),n&&w(n,!0),a(),f(),v.value=!0}}function z(){s.value||c.value||x()}function y(){z(),l.value&&(l.value.checked=c.value)}function B(){b.value=!1}function R(){b.value=!0}return{mergedClsPrefix:o?o.mergedClsPrefixRef:t,inputRef:l,labelRef:u,mergedName:m,mergedDisabled:s,renderSafeChecked:c,focus:b,mergedSize:h,handleRadioInputChange:y,handleRadioInputBlur:B,handleRadioInputFocus:R}}const he=T("radio-group",`
 display: inline-block;
 font-size: var(--n-font-size);
`,[C("splitor",`
 display: inline-block;
 vertical-align: bottom;
 width: 1px;
 transition:
 background-color .3s var(--n-bezier),
 opacity .3s var(--n-bezier);
 background: var(--n-button-border-color);
 `,[S("checked",{backgroundColor:"var(--n-button-border-color-active)"}),S("disabled",{opacity:"var(--n-opacity-disabled)"})]),S("button-group",`
 white-space: nowrap;
 height: var(--n-height);
 line-height: var(--n-height);
 `,[T("radio-button",{height:"var(--n-height)",lineHeight:"var(--n-height)"}),C("splitor",{height:"var(--n-height)"})]),T("radio-button",`
 vertical-align: bottom;
 outline: none;
 position: relative;
 user-select: none;
 -webkit-user-select: none;
 display: inline-block;
 box-sizing: border-box;
 padding-left: 14px;
 padding-right: 14px;
 white-space: nowrap;
 transition:
 background-color .3s var(--n-bezier),
 opacity .3s var(--n-bezier),
 border-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 background: var(--n-button-color);
 color: var(--n-button-text-color);
 border-top: 1px solid var(--n-button-border-color);
 border-bottom: 1px solid var(--n-button-border-color);
 `,[T("radio-input",`
 pointer-events: none;
 position: absolute;
 border: 0;
 border-radius: inherit;
 left: 0;
 right: 0;
 top: 0;
 bottom: 0;
 opacity: 0;
 z-index: 1;
 `),C("state-border",`
 z-index: 1;
 pointer-events: none;
 position: absolute;
 box-shadow: var(--n-button-box-shadow);
 transition: box-shadow .3s var(--n-bezier);
 left: -1px;
 bottom: -1px;
 right: -1px;
 top: -1px;
 `),V("&:first-child",`
 border-top-left-radius: var(--n-button-border-radius);
 border-bottom-left-radius: var(--n-button-border-radius);
 border-left: 1px solid var(--n-button-border-color);
 `,[C("state-border",`
 border-top-left-radius: var(--n-button-border-radius);
 border-bottom-left-radius: var(--n-button-border-radius);
 `)]),V("&:last-child",`
 border-top-right-radius: var(--n-button-border-radius);
 border-bottom-right-radius: var(--n-button-border-radius);
 border-right: 1px solid var(--n-button-border-color);
 `,[C("state-border",`
 border-top-right-radius: var(--n-button-border-radius);
 border-bottom-right-radius: var(--n-button-border-radius);
 `)]),A("disabled",`
 cursor: pointer;
 `,[V("&:hover",[C("state-border",`
 transition: box-shadow .3s var(--n-bezier);
 box-shadow: var(--n-button-box-shadow-hover);
 `),A("checked",{color:"var(--n-button-text-color-hover)"})]),S("focus",[V("&:not(:active)",[C("state-border",{boxShadow:"var(--n-button-box-shadow-focus)"})])])]),S("checked",`
 background: var(--n-button-color-active);
 color: var(--n-button-text-color-active);
 border-color: var(--n-button-border-color-active);
 `),S("disabled",`
 cursor: not-allowed;
 opacity: var(--n-opacity-disabled);
 `)])]);function ve(e,o,t){var i;const d=[];let h=!1;for(let s=0;s<e.length;++s){const l=e[s],u=(i=l.type)===null||i===void 0?void 0:i.name;u==="RadioButton"&&(h=!0);const v=l.props;if(u!=="RadioButton"){d.push(l);continue}if(s===0)d.push(l);else{const g=d[d.length-1].props,p=o===g.value,c=g.disabled,m=o===v.value,b=v.disabled,x=(p?2:0)+(c?0:1),z=(m?2:0)+(b?0:1),y={[`${t}-radio-group__splitor--disabled`]:c,[`${t}-radio-group__splitor--checked`]:p},B={[`${t}-radio-group__splitor--disabled`]:b,[`${t}-radio-group__splitor--checked`]:m},R=x<z?B:y;d.push(G("div",{class:[`${t}-radio-group__splitor`,R]}),l)}}return{children:d,isButtonGroup:h}}const fe=Object.assign(Object.assign({},j.props),{name:String,value:[String,Number,Boolean],defaultValue:{type:[String,Number,Boolean],default:null},size:String,disabled:{type:Boolean,default:void 0},"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array]}),Re=ae({name:"RadioGroup",props:fe,setup(e){const o=k(null),{mergedSizeRef:t,mergedDisabledRef:i,nTriggerFormChange:d,nTriggerFormInput:h,nTriggerFormBlur:s,nTriggerFormFocus:l}=P(e),{mergedClsPrefixRef:u,inlineThemeDisabled:v,mergedRtlRef:g}=E(e),p=j("Radio","-radio-group",he,be,e,u),c=k(e.defaultValue),m=$(e,"value"),b=M(m,c);function x(n){const{onUpdateValue:a,"onUpdate:value":f}=e;a&&w(a,n),f&&w(f,n),c.value=n,d(),h()}function z(n){const{value:a}=o;a&&(a.contains(n.relatedTarget)||l())}function y(n){const{value:a}=o;a&&(a.contains(n.relatedTarget)||s())}le(N,{mergedClsPrefixRef:u,nameRef:$(e,"name"),valueRef:b,disabledRef:i,mergedSizeRef:t,doUpdateValue:x});const B=de("Radio",g,u),R=H(()=>{const{value:n}=t,{common:{cubicBezierEaseInOut:a},self:{buttonBorderColor:f,buttonBorderColorActive:F,buttonBorderRadius:I,buttonBoxShadow:L,buttonBoxShadowFocus:O,buttonBoxShadowHover:K,buttonColor:J,buttonColorActive:X,buttonTextColor:Z,buttonTextColorActive:q,buttonTextColorHover:Q,opacityDisabled:W,[U("buttonHeight",n)]:Y,[U("fontSize",n)]:ee}}=p.value;return{"--n-font-size":ee,"--n-bezier":a,"--n-button-border-color":f,"--n-button-border-color-active":F,"--n-button-border-radius":I,"--n-button-box-shadow":L,"--n-button-box-shadow-focus":O,"--n-button-box-shadow-hover":K,"--n-button-color":J,"--n-button-color-active":X,"--n-button-text-color":Z,"--n-button-text-color-hover":Q,"--n-button-text-color-active":q,"--n-height":Y,"--n-opacity-disabled":W}}),r=v?se("radio-group",H(()=>t.value[0]),R,e):void 0;return{selfElRef:o,rtlEnabled:B,mergedClsPrefix:u,mergedValue:b,handleFocusout:y,handleFocusin:z,cssVars:v?void 0:R,themeClass:r==null?void 0:r.themeClass,onRender:r==null?void 0:r.onRender}},render(){var e;const{mergedValue:o,mergedClsPrefix:t,handleFocusin:i,handleFocusout:d}=this,{children:h,isButtonGroup:s}=ve(ie(ue(this)),o,t);return(e=this.onRender)===null||e===void 0||e.call(this),G("div",{onFocusin:i,onFocusout:d,ref:"selfElRef",class:[`${t}-radio-group`,this.rtlEnabled&&`${t}-radio-group--rtl`,this.themeClass,s&&`${t}-radio-group--button-group`],style:this.cssVars},h)}});export{Re as N,xe as a,be as r,Ce as s};
