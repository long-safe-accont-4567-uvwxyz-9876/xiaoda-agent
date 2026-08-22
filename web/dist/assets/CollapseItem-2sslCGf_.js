import{d as R,Q as l,a8 as f,a9 as x,aa as s,a6 as z,bC as H,b8 as V,X as _,r as W,ae as T,Y as L,ag as O,l as N,bD as q,bb as K,a2 as Z,aj as S,bE as G,i as J,bF as Q,al as k,V as X,bG as A,bH as Y,bI as ee,bJ as re,an as ae,bd as te,W as le}from"./index-CRuQ54NC.js";import{u as oe}from"./Suffix-Di-HDVuf.js";import{h as D}from"./FocusDetector-CJm8UxU2.js";const se=R({name:"ChevronLeft",render(){return l("svg",{viewBox:"0 0 16 16",fill:"none",xmlns:"http://www.w3.org/2000/svg"},l("path",{d:"M10.3536 3.14645C10.5488 3.34171 10.5488 3.65829 10.3536 3.85355L6.20711 8L10.3536 12.1464C10.5488 12.3417 10.5488 12.6583 10.3536 12.8536C10.1583 13.0488 9.84171 13.0488 9.64645 12.8536L5.14645 8.35355C4.95118 8.15829 4.95118 7.84171 5.14645 7.64645L9.64645 3.14645C9.84171 2.95118 10.1583 2.95118 10.3536 3.14645Z",fill:"currentColor"}))}}),ne=R({name:"ChevronRight",render(){return l("svg",{viewBox:"0 0 16 16",fill:"none",xmlns:"http://www.w3.org/2000/svg"},l("path",{d:"M5.64645 3.14645C5.45118 3.34171 5.45118 3.65829 5.64645 3.85355L9.79289 8L5.64645 12.1464C5.45118 12.3417 5.45118 12.6583 5.64645 12.8536C5.84171 13.0488 6.15829 13.0488 6.35355 12.8536L10.8536 8.35355C11.0488 8.15829 11.0488 7.84171 10.8536 7.64645L6.35355 3.14645C6.15829 2.95118 5.84171 2.95118 5.64645 3.14645Z",fill:"currentColor"}))}}),ie=f("collapse","width: 100%;",[f("collapse-item",`
 font-size: var(--n-font-size);
 color: var(--n-text-color);
 transition:
 color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
 margin: var(--n-item-margin);
 `,[x("disabled",[s("header","cursor: not-allowed;",[s("header-main",`
 color: var(--n-title-text-color-disabled);
 `),f("collapse-item-arrow",`
 color: var(--n-arrow-color-disabled);
 `)])]),f("collapse-item","margin-left: 32px;"),z("&:first-child","margin-top: 0;"),z("&:first-child >",[s("header","padding-top: 0;")]),x("left-arrow-placement",[s("header",[f("collapse-item-arrow","margin-right: 4px;")])]),x("right-arrow-placement",[s("header",[f("collapse-item-arrow","margin-left: 4px;")])]),s("content-wrapper",[s("content-inner","padding-top: 16px;"),H({duration:"0.15s"})]),x("active",[s("header",[x("active",[f("collapse-item-arrow","transform: rotate(90deg);")])])]),z("&:not(:first-child)","border-top: 1px solid var(--n-divider-color);"),V("disabled",[x("trigger-area-main",[s("header",[s("header-main","cursor: pointer;"),f("collapse-item-arrow","cursor: default;")])]),x("trigger-area-arrow",[s("header",[f("collapse-item-arrow","cursor: pointer;")])]),x("trigger-area-extra",[s("header",[s("header-extra","cursor: pointer;")])])]),s("header",`
 font-size: var(--n-title-font-size);
 display: flex;
 flex-wrap: nowrap;
 align-items: center;
 transition: color .3s var(--n-bezier);
 position: relative;
 padding: var(--n-title-padding);
 color: var(--n-title-text-color);
 `,[s("header-main",`
 display: flex;
 flex-wrap: nowrap;
 align-items: center;
 font-weight: var(--n-title-font-weight);
 transition: color .3s var(--n-bezier);
 flex: 1;
 color: var(--n-title-text-color);
 `),s("header-extra",`
 display: flex;
 align-items: center;
 transition: color .3s var(--n-bezier);
 color: var(--n-text-color);
 `),f("collapse-item-arrow",`
 display: flex;
 transition:
 transform .15s var(--n-bezier),
 color .3s var(--n-bezier);
 font-size: 18px;
 color: var(--n-arrow-color);
 `)])])]),de=Object.assign(Object.assign({},T.props),{defaultExpandedNames:{type:[Array,String],default:null},expandedNames:[Array,String],arrowPlacement:{type:String,default:"left"},accordion:{type:Boolean,default:!1},displayDirective:{type:String,default:"if"},triggerAreas:{type:Array,default:()=>["main","extra","arrow"]},onItemHeaderClick:[Function,Array],"onUpdate:expandedNames":[Function,Array],onUpdateExpandedNames:[Function,Array],onExpandedNamesChange:{type:[Function,Array],validator:()=>!0,default:void 0}}),F=K("n-collapse"),he=R({name:"Collapse",props:de,slots:Object,setup(e,{slots:i}){const{mergedClsPrefixRef:n,inlineThemeDisabled:o,mergedRtlRef:d}=_(e),a=W(e.defaultExpandedNames),h=N(()=>e.expandedNames),v=oe(h,a),w=T("Collapse","-collapse",ie,q,e,n);function c(p){const{"onUpdate:expandedNames":t,onUpdateExpandedNames:m,onExpandedNamesChange:y}=e;m&&S(m,p),t&&S(t,p),y&&S(y,p),a.value=p}function g(p){const{onItemHeaderClick:t}=e;t&&S(t,p)}function r(p,t,m){const{accordion:y}=e,{value:I}=v;if(y)p?(c([t]),g({name:t,expanded:!0,event:m})):(c([]),g({name:t,expanded:!1,event:m}));else if(!Array.isArray(I))c([t]),g({name:t,expanded:!0,event:m});else{const C=I.slice(),P=C.findIndex($=>t===$);~P?(C.splice(P,1),c(C),g({name:t,expanded:!1,event:m})):(C.push(t),c(C),g({name:t,expanded:!0,event:m}))}}Z(F,{props:e,mergedClsPrefixRef:n,expandedNamesRef:v,slots:i,toggleItem:r});const u=L("Collapse",d,n),E=N(()=>{const{common:{cubicBezierEaseInOut:p},self:{titleFontWeight:t,dividerColor:m,titlePadding:y,titleTextColor:I,titleTextColorDisabled:C,textColor:P,arrowColor:$,fontSize:B,titleFontSize:j,arrowColorDisabled:M,itemMargin:U}}=w.value;return{"--n-font-size":B,"--n-bezier":p,"--n-text-color":P,"--n-divider-color":m,"--n-title-padding":y,"--n-title-font-size":j,"--n-title-text-color":I,"--n-title-text-color-disabled":C,"--n-title-font-weight":t,"--n-arrow-color":$,"--n-arrow-color-disabled":M,"--n-item-margin":U}}),b=o?O("collapse",void 0,E,e):void 0;return{rtlEnabled:u,mergedTheme:w,mergedClsPrefix:n,cssVars:o?void 0:E,themeClass:b==null?void 0:b.themeClass,onRender:b==null?void 0:b.onRender}},render(){var e;return(e=this.onRender)===null||e===void 0||e.call(this),l("div",{class:[`${this.mergedClsPrefix}-collapse`,this.rtlEnabled&&`${this.mergedClsPrefix}-collapse--rtl`,this.themeClass],style:this.cssVars},this.$slots)}}),ce=R({name:"CollapseItemContent",props:{displayDirective:{type:String,required:!0},show:Boolean,clsPrefix:{type:String,required:!0}},setup(e){return{onceTrue:Q(k(e,"show"))}},render(){return l(G,null,{default:()=>{const{show:e,displayDirective:i,onceTrue:n,clsPrefix:o}=this,d=i==="show"&&n,a=l("div",{class:`${o}-collapse-item__content-wrapper`},l("div",{class:`${o}-collapse-item__content-inner`},this.$slots));return d?J(a,[[X,e]]):e?a:null}})}}),pe={title:String,name:[String,Number],disabled:Boolean,displayDirective:String},ge=R({name:"CollapseItem",props:pe,setup(e){const{mergedRtlRef:i}=_(e),n=ee(),o=re(()=>{var r;return(r=e.name)!==null&&r!==void 0?r:n}),d=le(F);d||ae("collapse-item","`n-collapse-item` must be placed inside `n-collapse`.");const{expandedNamesRef:a,props:h,mergedClsPrefixRef:v,slots:w}=d,c=N(()=>{const{value:r}=a;if(Array.isArray(r)){const{value:u}=o;return!~r.findIndex(E=>E===u)}else if(r){const{value:u}=o;return u!==r}return!0});return{rtlEnabled:L("Collapse",i,v),collapseSlots:w,randomName:n,mergedClsPrefix:v,collapsed:c,triggerAreas:k(h,"triggerAreas"),mergedDisplayDirective:N(()=>{const{displayDirective:r}=e;return r||h.displayDirective}),arrowPlacement:N(()=>h.arrowPlacement),handleClick(r){let u="main";D(r,"arrow")&&(u="arrow"),D(r,"extra")&&(u="extra"),h.triggerAreas.includes(u)&&d&&!e.disabled&&d.toggleItem(c.value,o.value,r)}}},render(){const{collapseSlots:e,$slots:i,arrowPlacement:n,collapsed:o,mergedDisplayDirective:d,mergedClsPrefix:a,disabled:h,triggerAreas:v}=this,w=A(i.header,{collapsed:o},()=>[this.title]),c=i["header-extra"]||e["header-extra"],g=i.arrow||e.arrow;return l("div",{class:[`${a}-collapse-item`,`${a}-collapse-item--${n}-arrow-placement`,h&&`${a}-collapse-item--disabled`,!o&&`${a}-collapse-item--active`,v.map(r=>`${a}-collapse-item--trigger-area-${r}`)]},l("div",{class:[`${a}-collapse-item__header`,!o&&`${a}-collapse-item__header--active`]},l("div",{class:`${a}-collapse-item__header-main`,onClick:this.handleClick},n==="right"&&w,l("div",{class:`${a}-collapse-item-arrow`,key:this.rtlEnabled?0:1,"data-arrow":!0},A(g,{collapsed:o},()=>[l(te,{clsPrefix:a},{default:()=>this.rtlEnabled?l(se,null):l(ne,null)})])),n==="left"&&w),Y(c,{collapsed:o},r=>l("div",{class:`${a}-collapse-item__header-extra`,onClick:this.handleClick,"data-extra":!0},r))),l(ce,{clsPrefix:a,displayDirective:d,show:!o},i))}});export{he as N,ge as a};
