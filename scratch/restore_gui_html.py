# -*- coding: utf-8 -*-
with open("demo/ycode_gui_demo.html", "w", encoding="utf-8") as f:
    f.write("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Y-CODE · 星瑶 — GUI 编码助手</title>
<style>
:root{
  --bg0:#0d1117; --bg1:#161b22; --bg2:#1c2330; --bg3:#232b3a;
  --border:#2d3646; --text:#d7dde8; --text-dim:#8b98ab; --text-faint:#5b6678;
  --accent:#4f8cff; --accent2:#7c5cff; --green:#3fb950; --yellow:#d29922;
  --red:#f85149; --orange:#f0883e; --cyan:#39c5cf; --purple:#bc8cff;
  --mono:'JetBrains Mono','Cascadia Code','Consolas',monospace;
  --sans:'Segoe UI','Microsoft YaHei',system-ui,sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg0);color:var(--text);font-family:var(--sans);font-size:14px;height:100vh;display:flex;flex-direction:column;overflow:hidden}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#3a4356;border-radius:4px}
::-webkit-scrollbar-track{background:transparent}
button{font-family:inherit;cursor:pointer}
.mono{font-family:var(--mono)}

/* ===== 顶栏 ===== */
#topbar{height:48px;background:var(--bg1);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 14px;gap:12px;flex-shrink:0}
#topbar .logo{display:flex;align-items:center;gap:9px;user-select:none}
#topbar .logo .mark{width:26px;height:26px;border-radius:7px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff;font-size:13px;box-shadow:0 0 12px rgba(79,140,255,.4)}
#topbar .logo .name{font-weight:600;letter-spacing:.5px}
#topbar .logo .sub{color:var(--text-faint);font-size:12px}
.top-spacer{flex:1}
.pill{background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:5px 10px;font-size:12.5px;display:flex;align-items:center;gap:6px;transition:.15s}
.pill:hover{border-color:var(--accent);background:var(--bg3)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green)}
.pill .caret{color:var(--text-faint);font-size:10px}
.mode-pill .mode-dot{width:7px;height:7px;border-radius:50%;background:var(--yellow);box-shadow:0 0 6px var(--yellow)}
.icon-btn{background:transparent;border:1px solid transparent;color:var(--text-dim);border-radius:7px;width:32px;height:32px;font-size:15px;display:flex;align-items:center;justify-content:center;transition:.15s}
.icon-btn:hover{background:var(--bg3);color:var(--text);border-color:var(--border)}
.icon-btn.active{color:var(--accent);border-color:var(--accent)}

/* ===== 主体三栏 ===== */
#main{flex:1;display:flex;min-height:0}

/* --- 左侧栏 --- */
#sidebar{width:240px;background:var(--bg1);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;transition:margin-left .25s}
#sidebar.collapsed{margin-left:-240px}
.side-section{padding:10px 10px 4px;color:var(--text-faint);font-size:11px;letter-spacing:1px;text-transform:uppercase;display:flex;justify-content:space-between;align-items:center}
.side-section .add-btn{background:none;border:none;color:var(--text-faint);font-size:14px;padding:0 4px;border-radius:4px}
.side-section .add-btn:hover{color:var(--accent)}
#session-list{padding:0 6px}
.session-item{padding:8px 10px;border-radius:7px;cursor:pointer;display:flex;flex-direction:column;gap:2px;margin-bottom:2px;border:1px solid transparent}
.session-item:hover{background:var(--bg2)}
.session-item.active{background:var(--bg3);border-color:var(--border)}
.session-item .t{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.session-item .m{font-size:11px;color:var(--text-faint)}
.side-scroll{flex:1;overflow-y:auto}
.skill-chip,.mcp-item{display:flex;align-items:center;gap:8px;padding:6px 10px;margin:0 6px 2px;border-radius:7px;cursor:pointer;font-size:12.5px;color:var(--text-dim)}
.skill-chip:hover,.mcp-item:hover{background:var(--bg2);color:var(--text)}
.skill-chip .ico{font-size:13px}
.mcp-item .st{width:7px;height:7px;border-radius:50%;background:var(--green)}
.mcp-item .st.off{background:var(--text-faint)}
.side-footer{padding:10px;border-top:1px solid var(--border);font-size:12px;color:var(--text-faint)}

/* --- 中间聊天区 --- */
#center{flex:1;display:flex;flex-direction:column;min-width:0;position:relative}
#chat-header{height:38px;border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 16px;gap:10px;background:var(--bg1);flex-shrink:0}
#chat-header .crumb{font-size:13px;color:var(--text-dim)}
#chat-header .crumb b{color:var(--text)}
#chat-scroll{flex:1;overflow-y:auto;padding:20px 28px 10px}
.msg{max-width:880px;margin:0 auto 18px}
.msg-user{display:flex;justify-content:flex-end}
.msg-user .bubble{background:linear-gradient(135deg,#274060,#233a63);border:1px solid #33507e;border-radius:12px 12px 4px 12px;padding:10px 14px;max-width:70%}
.msg-assistant .bubble{max-width:100%}
.avatar-row{display:flex;gap:10px}
.avatar{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;color:#fff}
.avatar.user{background:linear-gradient(135deg,#3f6f4f,#2c5340)}
.assistant-body{flex:1;min-width:0}
.assistant-name{font-size:12px;color:var(--text-faint);margin-bottom:4px}
.assistant-name b{color:var(--text-dim)}
.md-body{line-height:1.7}
.md-body p{margin:6px 0}
.md-body h4{margin:12px 0 4px;color:#fff}
.md-body code{background:var(--bg3);padding:1px 5px;border-radius:4px;font-family:var(--mono);font-size:12.5px;color:var(--cyan)}
.md-body pre{background:#0a0e14;border:1px solid var(--border);border-radius:8px;padding:12px;margin:8px 0;overflow-x:auto;font-family:var(--mono);font-size:12.5px;line-height:1.55}
.md-body pre code{background:none;padding:0;color:var(--text)}
.md-body ul{padding-left:20px;margin:6px 0}
.md-body li{margin:3px 0}
.cursor-blink{display:inline-block;width:8px;height:15px;background:var(--accent);vertical-align:-2px;animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}

/* 思考块 */
.think-block{border-left:2px solid var(--purple);background:rgba(188,140,255,.06);border-radius:0 8px 8px 0;padding:8px 12px;margin:8px 0;font-size:12.5px;color:#b8a5d6}
.think-block .tk{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--purple);margin-bottom:4px;cursor:pointer;user-select:none}
.think-block .tk:hover{color:#d2b8ff}
.think-block .tk-cnt{white-space:pre-wrap;font-family:var(--mono);font-size:12px}

/* 工具卡片 */
.tool-card{border:1px solid var(--border);border-radius:9px;background:var(--bg1);margin:8px 0;overflow:hidden;animation:fadein .3s}
@keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.tool-head{display:flex;align-items:center;gap:8px;padding:7px 12px;cursor:pointer;user-select:none;font-family:var(--mono);font-size:12.5px}
.tool-head:hover{background:var(--bg2)}
.tool-head .tico{width:20px;text-align:center}
.tool-head .tname{color:#fff;font-weight:600}
.tool-head .targ{color:var(--text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.badge{font-size:10.5px;padding:2px 8px;border-radius:10px;font-family:var(--sans);flex-shrink:0}
.badge.run{background:rgba(210,153,34,.15);color:var(--yellow)}
.badge.ok{background:rgba(63,185,80,.15);color:var(--green)}
.badge.err{background:rgba(248,81,73,.15);color:var(--red)}
.spin{display:inline-block;width:11px;height:11px;border:2px solid var(--yellow);border-top-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.tool-body{border-top:1px solid var(--border);padding:10px 12px;font-family:var(--mono);font-size:12px;line-height:1.6;white-space:pre-wrap;color:var(--text-dim);background:#0a0e14;display:none;max-height:300px;overflow-y:auto}
.tool-card.open .tool-body{display:block}

/* 输入区 */
#input-area{border-top:1px solid var(--border);background:var(--bg1);padding:12px 28px 10px;flex-shrink:0}
#input-wrap{max-width:880px;margin:0 auto}
#input-box{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:10px 12px;transition:border-color .15s}
#input-box:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px rgba(79,140,255,.12)}
#input-box textarea{width:100%;background:transparent;border:none;outline:none;color:var(--text);font-family:inherit;font-size:14px;resize:none;line-height:1.5;max-height:140px}
#input-box textarea::placeholder{color:var(--text-faint)}
#input-bar{display:flex;align-items:center;gap:8px;margin-top:6px}
.chip-btn{background:var(--bg3);border:1px solid var(--border);color:var(--text-dim);border-radius:7px;padding:4px 10px;font-size:12px;display:flex;align-items:center;gap:5px;transition:.15s}
.chip-btn:hover{color:var(--text);border-color:var(--accent)}
.chip-btn.on{color:var(--accent);border-color:var(--accent);background:rgba(79,140,255,.1)}
#send-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;border-radius:8px;padding:6px 16px;font-size:13px;font-weight:600;margin-left:auto;transition:.15s;display:flex;align-items:center;gap:6px}
#send-btn:hover{filter:brightness(1.15);box-shadow:0 0 14px rgba(79,140,255,.4)}
#send-btn.stop{background:linear-gradient(135deg,#a33,#c0392b)}
#input-meta{max-width:880px;margin:7px auto 0;display:flex;justify-content:space-between;font-size:11.5px;color:var(--text-faint)}
#input-meta .l span{margin-right:14px}
.kbd{background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:0 5px;font-family:var(--mono);font-size:10.5px}

/* --- 右侧栏 --- */
#rightbar{width:330px;background:var(--bg1);border-left:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;transition:margin-right .25s}
#rightbar.collapsed{margin-right:-330px}
#right-tabs{display:flex;border-bottom:1px solid var(--border);flex-shrink:0}
.rtab{flex:1;padding:9px 0;text-align:center;font-size:12.5px;color:var(--text-dim);cursor:pointer;border-bottom:2px solid transparent;background:none;border-top:none;border-left:none;border-right:none;transition:.15s}
.rtab:hover{color:var(--text)}
.rtab.active{color:var(--accent);border-bottom-color:var(--accent)}
#right-content{flex:1;overflow-y:auto;padding:12px}
.rpane{display:none}
.rpane.active{display:block}

/* 计划面板 */
.plan-item{display:flex;gap:9px;padding:9px 10px;border:1px solid var(--border);border-radius:9px;margin-bottom:8px;background:var(--bg2);cursor:pointer;transition:.15s}
.plan-item:hover{border-color:var(--accent)}
.plan-item .pstat{width:18px;height:18px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:11px;margin-top:1px}
.plan-item.done .pstat{background:var(--green);color:#0d1117}
.plan-item.doing .pstat{border:2px solid var(--yellow);color:var(--yellow)}
.plan-item.todo .pstat{border:2px solid var(--text-faint);color:var(--text-faint)}
.plan-item .pt{font-size:12.5px;line-height:1.45}
.plan-item .pm{font-size:11px;color:var(--text-faint);margin-top:2px}
.plan-item.done .pt{color:var(--text-dim);text-decoration:line-through}
.progress-line{display:flex;justify-content:space-between;font-size:11.5px;color:var(--text-faint);margin:2px 0 8px}
.progress-bar{height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;margin-bottom:12px}
.progress-bar i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--green));border-radius:3px;transition:width .5s}

/* 后台任务 */
.bg-task{border:1px solid var(--border);border-radius:9px;margin-bottom:8px;background:var(--bg2);overflow:hidden}
.bg-task .bth{display:flex;align-items:center;gap:8px;padding:8px 10px;cursor:pointer;font-size:12.5px}
.bg-task .bth:hover{background:var(--bg3)}
.bg-task .btd{color:var(--text-faint);font-size:11px;margin-left:auto}
.bg-task .btlog{border-top:1px solid var(--border);background:#0a0e14;padding:9px 10px;font-family:var(--mono);font-size:11.5px;line-height:1.6;color:var(--text-dim);white-space:pre-wrap;display:none;max-height:180px;overflow-y:auto}
.bg-task.open .btlog{display:block}
.bt-kill{background:none;border:1px solid var(--border);color:var(--text-faint);border-radius:5px;font-size:10.5px;padding:2px 7px}
.bt-kill:hover{color:var(--red);border-color:var(--red)}

/* 终端 */
#pane-terminal .term-body{background:#0a0e14;border:1px solid var(--border);border-radius:9px;padding:12px;font-family:var(--mono);font-size:12px;line-height:1.65;min-height:320px}
#pane-terminal .t-out{white-space:pre-wrap;color:var(--text-dim)}
#pane-terminal .t-cmd{color:#fff}
#pane-terminal .t-ok{color:var(--green)}
#pane-terminal .t-warn{color:var(--yellow)}
.term-input-row{display:flex;gap:6px;margin-top:4px;color:var(--green)}
.term-input-row input{flex:1;background:none;border:none;outline:none;color:#fff;font-family:var(--mono);font-size:12px}

/* 文件树 */
.file-row{display:flex;align-items:center;gap:7px;padding:4px 8px;border-radius:6px;cursor:pointer;font-size:12.5px;color:var(--text-dim)}
.file-row:hover{background:var(--bg2);color:var(--text)}
.file-row .fi{width:15px;text-align:center;font-size:12px}
.file-children{margin-left:16px;border-left:1px solid var(--border);padding-left:6px;display:none}
.file-row.open+.file-children{display:block}

/* ===== 权限弹窗 ===== */
.modal-mask{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:100;backdrop-filter:blur(2px)}
.modal-mask.show{display:flex}
.modal{width:480px;background:var(--bg1);border:1px solid var(--border);border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.6);overflow:hidden;animation:pop .2s}
@keyframes pop{from{transform:scale(.95);opacity:0}to{transform:none;opacity:1}}
.modal-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.modal-head .mh-ico{width:32px;height:32px;border-radius:8px;background:rgba(240,136,62,.15);display:flex;align-items:center;justify-content:center;font-size:16px}
.modal-head h3{font-size:14.5px}
.modal-head p{font-size:12px;color:var(--text-faint)}
.modal-body{padding:16px 18px;font-family:var(--mono);font-size:12.5px;background:#0a0e14;white-space:pre-wrap;line-height:1.65;color:var(--text-dim);max-height:240px;overflow-y:auto}
.modal-foot{padding:13px 18px;display:flex;gap:9px;justify-content:flex-end}
.btn{border-radius:8px;padding:7px 16px;font-size:13px;border:1px solid var(--border);background:var(--bg3);color:var(--text);transition:.15s}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;font-weight:600}
.btn.danger{color:var(--red)}
.btn.danger:hover{border-color:var(--red);background:rgba(248,81,73,.08)}

/* 通用下拉 */
#dropdown{position:fixed;background:var(--bg2);border:1px solid var(--border);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.5);z-index:90;min-width:200px;display:none;padding:5px}
#dropdown.show{display:block}
.dd-item{padding:8px 12px;border-radius:7px;cursor:pointer;font-size:12.5px;display:flex;align-items:center;gap:8px}
.dd-item:hover{background:var(--bg3)}
.dd-item .ck{margin-left:auto;color:var(--accent)}
.dd-sep{height:1px;background:var(--border);margin:4px 6px}

/* Toast */
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);background:var(--bg2);border:1px solid var(--border);border-radius:9px;padding:10px 18px;font-size:13px;z-index:200;transition:transform .3s;box-shadow:0 8px 24px rgba(0,0,0,.4)}
#toast.show{transform:translateX(-50%)}
#toast.ok{border-color:var(--green)}
#toast.warn{border-color:var(--orange)}

/* 状态栏 */
#statusbar{height:24px;background:var(--bg1);border-top:1px solid var(--border);display:flex;align-items:center;padding:0 12px;gap:16px;font-size:11px;color:var(--text-faint);flex-shrink:0}
#statusbar .si{display:flex;align-items:center;gap:5px}
#statusbar .right{margin-left:auto;display:flex;gap:16px}
.s-dot{width:6px;height:6px;border-radius:50%;background:var(--green)}

/* 左侧菜单项 */
.side-menu{padding:8px 8px 2px;border-bottom:1px solid var(--border)}
.menu-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;cursor:pointer;color:var(--text-dim);margin-bottom:3px;transition:.15s}
.menu-item:hover{background:var(--bg2);color:var(--text)}
.menu-item .mi-ico{font-size:14px;width:18px;text-align:center}
.menu-item .mi-label{font-size:13px;font-weight:500}
.menu-item .mi-count{margin-left:auto;font-size:10.5px;color:var(--text-faint);background:var(--bg3);border-radius:8px;padding:1px 7px}
.menu-item .mi-arrow{color:var(--text-faint);font-size:14px}

/* 会话文件夹 */
.folder-item{margin-bottom:2px}
.folder-row{display:flex;align-items:center;gap:6px;padding:6px 10px;border-radius:7px;cursor:pointer;color:var(--text-dim);font-size:12.5px;user-select:none}
.folder-row:hover{background:var(--bg2);color:var(--text)}
.folder-row .f-arrow{font-size:10px;width:10px;color:var(--text-faint)}
.folder-row .f-ico{font-size:13px}
.folder-row .f-name{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.folder-row .f-actions{display:none;gap:2px}
.folder-row:hover .f-actions{display:flex}
.f-btn{background:none;border:none;color:var(--text-faint);font-size:11px;padding:1px 4px;border-radius:4px}
.f-btn:hover{color:var(--accent);background:var(--bg3)}
.folder-children{margin-left:12px;border-left:1px solid var(--border);padding-left:5px}

/* 二级页面 */
#subpage{position:absolute;inset:0;background:var(--bg0);z-index:50;display:none;flex-direction:column}
#subpage.show{display:flex}
.sub-head{height:52px;border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:12px;background:var(--bg1);flex-shrink:0}
.sub-back{background:var(--bg2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 14px;font-size:12.5px;display:flex;align-items:center;gap:6px}
.sub-back:hover{border-color:var(--accent);color:var(--accent)}
.sub-head h2{font-size:15px}
.sub-head .sub-desc{font-size:12px;color:var(--text-faint)}
.sub-body{flex:1;overflow-y:auto;padding:22px 26px}
.sub-grid{max-width:960px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.sub-card{background:var(--bg1);border:1px solid var(--border);border-radius:12px;padding:16px;transition:.15s;cursor:pointer}
.sub-card:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.25)}
.sub-card .sc-top{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.sub-card .sc-ico{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--bg3),var(--bg2));border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:16px}
.sub-card .sc-name{font-size:13.5px;font-weight:600}
.sub-card .sc-scope{font-size:10.5px;color:var(--text-faint);background:var(--bg3);border-radius:7px;padding:1px 7px;margin-left:auto}
.sub-card .sc-desc{font-size:12px;color:var(--text-dim);line-height:1.6;min-height:38px}
.sub-card .sc-foot{display:flex;align-items:center;justify-content:space-between;margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:11.5px;color:var(--text-faint)}
.switch{position:relative;width:34px;height:19px;background:var(--bg3);border:1px solid var(--border);border-radius:11px;cursor:pointer;transition:.2s;flex-shrink:0}
.switch::after{content:'';position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;background:var(--text-faint);transition:.2s}
.switch.on{background:var(--accent);border-color:var(--accent)}
.switch.on::after{left:17px;background:#fff}
.mcp-card{cursor:default}
.mcp-card:hover{transform:none}
.mcp-tools{margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}
.mcp-tools .t-tool{display:flex;align-items:center;gap:7px;font-size:12px;font-family:var(--mono);color:var(--text-dim);padding:4px 0}
.mcp-tools .t-tool .t-desc{margin-left:auto;font-family:var(--sans);font-size:11px;color:var(--text-faint)}
.mcp-status{display:flex;align-items:center;gap:6px;font-size:11.5px}
.mcp-status .st{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green)}
.mcp-status.off .st{background:var(--text-faint);box-shadow:none}
.mcp-actions{display:flex;gap:7px;margin-top:12px}
.mcp-actions .btn{flex:1;padding:6px 0;font-size:12px}
.sub-add{max-width:960px;margin:0 auto 16px}
.sub-add .btn{width:100%;padding:10px;border-style:dashed;color:var(--text-dim)}
.sub-add .btn:hover{color:var(--accent)}

/* 浅色主题 */
body.light{--bg0:#f5f7fa;--bg1:#ffffff;--bg2:#eef1f5;--bg3:#e2e7ee;--border:#d3dae4;--text:#1f2733;--text-dim:#5b6a80;--text-faint:#93a0b4}
body.light .md-body pre,body.light .tool-body,body.light .modal-body,body.light .bg-task .btlog,body.light #pane-terminal .term-body{background:#f0f3f7}
body.light .tool-head .tname{color:#1f2733}
body.light .md-body h4{color:#1f2733}
body.light .msg-user .bubble{background:linear-gradient(135deg,#dbe7ff,#d3e0fb);border-color:#b8ccf0}
body.light .think-block{color:#6b5396}
</style>
</head>
<body>

<!-- ===== 顶栏 ===== -->
<div id="topbar">
  <button class="icon-btn" id="btn-toggle-side" title="切换侧栏">☰</button>
  <div class="logo">
    <div class="mark">Y</div>
    <div><span class="name">Y-CODE</span> <span class="sub">星瑶 · GUI Window</span></div>
  </div>
  <div class="top-spacer"></div>
  <button class="pill" id="btn-model"><span class="dot"></span><span id="model-name">agnes-2.0-flash</span><span class="caret">▼</span></button>
  <button class="pill mode-pill" id="btn-mode"><span class="mode-dot" id="mode-dot"></span><span id="mode-name">默认模式</span><span class="caret">▼</span></button>
  <button class="icon-btn" id="btn-theme" title="切换主题">🌙</button>
  <button class="icon-btn" id="btn-toggle-right" title="切换面板">▐</button>
  <button class="icon-btn" id="btn-settings" title="设置">⚙</button>
</div>

<div id="main">
  <!-- ===== 左侧栏 ===== -->
  <div id="sidebar">
    <div class="side-menu">
      <div class="menu-item" id="menu-skills"><span class="mi-ico">⚡</span><span class="mi-label">技能 Skills</span><span class="mi-count">4</span><span class="mi-arrow">›</span></div>
      <div class="menu-item" id="menu-mcp"><span class="mi-ico">🔌</span><span class="mi-label">MCP 服务</span><span class="mi-count">2 在线</span><span class="mi-arrow">›</span></div>
    </div>
    <div class="side-section">会话
      <span>
        <button class="add-btn" id="btn-new-folder" title="新建文件夹">🗀+</button>
        <button class="add-btn" id="btn-new-session" title="在默认目录新建会话">＋</button>
      </span>
    </div>
    <div class="side-scroll">
      <div id="session-list">
        <div class="folder-item" data-open="1">
          <div class="folder-row">
            <span class="f-arrow">▾</span><span class="f-ico">🗀</span><span class="f-name">工作项目</span>
            <span class="f-actions"><button class="f-btn" data-act="new" title="在此文件夹新建会话">＋</button><button class="f-btn" data-act="rename" title="重命名">✎</button><button class="f-btn" data-act="del" title="删除文件夹">✕</button></span>
          </div>
          <div class="folder-children">
            <div class="session-item active" data-title="修复 memory 压缩器 bug"><div class="t">修复 memory 压缩器 bug</div><div class="m">刚刚 · 12 条消息</div></div>
            <div class="session-item" data-title="重构 model_selector"><div class="t">重构 model_selector</div><div class="m">2 小时前 · 34 条消息</div></div>
          </div>
        </div>
        <div class="folder-item">
          <div class="folder-row">
            <span class="f-arrow">▸</span><span class="f-ico">🗀</span><span class="f-name">历史归档</span>
            <span class="f-actions"><button class="f-btn" data-act="new" title="在此文件夹新建会话">＋</button><button class="f-btn" data-act="rename" title="重命名">✎</button><button class="f-btn" data-act="del" title="删除文件夹">✕</button></span>
          </div>
          <div class="folder-children" style="display:none">
            <div class="session-item" data-title="发布 v1.2 打包流程"><div class="t">发布 v1.2 打包流程</div><div class="m">3 天前 · 56 条消息</div></div>
          </div>
        </div>
        <div class="session-item" data-title="添加 MCP 天气工具"><div class="t">添加 MCP 天气工具</div><div class="m">昨天 · 21 条消息</div></div>
      </div>
    </div>
    <div class="side-footer">📁 E:\fch\Y-code_tui<br>分支 main · SQLite 已连接</div>
  </div>

  <!-- ===== 中间聊天区 ===== -->
  <div id="center">
    <div id="chat-header">
      <span class="crumb">会话 / <b id="crumb-title">修复 memory 压缩器 bug</b></span>
      <span class="top-spacer"></span>
      <button class="chip-btn" id="btn-export" title="导出会话">⤓ 导出</button>
      <button class="chip-btn" id="btn-clear" title="清空会话">🗑 清空</button>
    </div>
    <div id="chat-scroll">
      <div id="chat-body">

        <div class="msg msg-user"><div class="bubble">memory_compressor.py 压缩历史会话的时候偶发丢消息，帮我排查一下</div></div>

        <div class="msg msg-assistant">
          <div class="avatar-row">
            <div class="avatar">✦</div>
            <div class="assistant-body">
              <div class="assistant-name"><b>星瑶</b> · agnes-2.0-flash · 12.4s</div>
              <div class="md-body"><p>好的，我先看一下压缩器的实现，定位可能丢消息的路径。</p></div>

              <div class="think-block">
                <div class="tk" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none'">💭 思考过程 · 3.2s <span style="margin-left:auto">▾</span></div>
                <div class="tk-cnt">用户反馈压缩历史会话时偶发丢消息。可能的原因：
1. 事务未提交（AGENTS.md 里专门强调过 conn.commit()）
2. 压缩时边界条件处理错误，把未压缩消息误删
3. 多线程读写竞争
先读 memory_compressor.py 确认删除逻辑。</div>
              </div>

              <div class="tool-card">
                <div class="tool-head" onclick="toggleTool(this)"><span class="tico">📄</span><span class="tname">read_file</span><span class="targ">core/memory_compressor.py (1-180 行)</span><span class="badge ok">✓ 完成</span></div>
                <div class="tool-body"> 98  def compress_session(self, session_id):
 99      rows = self._fetch_uncompressed(session_id)
100      if len(rows) &lt; MIN_COMPRESS_BATCH:
101          return None
102      summary = self._summarize(rows)
103      cur = self.conn.cursor()
104      cur.execute("DELETE FROM messages WHERE session_id=? AND compressed=0", (session_id,))
105      cur.execute("INSERT INTO summaries ...")
106      # 注意: 此处缺少 conn.commit()  ← 问题点</div>
              </div>

              <div class="tool-card">
                <div class="tool-head" onclick="toggleTool(this)"><span class="tico">🔍</span><span class="tname">grep</span><span class="targ">"compressed=0" core/*.py</span><span class="badge ok">✓ 4 处匹配</span></div>
                <div class="tool-body">core/memory_compressor.py:104
core/memory.py:212
core/memory.py:245
core/context_builder.py:88</div>
              </div>

              <div class="md-body">
                <h4>问题定位</h4>
                <p>找到两个叠加原因：</p>
                <ul>
                  <li><code>compress_session</code> 第 104 行先 DELETE 再 INSERT summary，如果 INSERT 抛异常，消息已被删除且没有事务回滚 → 消息永久丢失。</li>
                  <li>整个函数缺少 <code>conn.commit()</code>，遇到进程退出时删除操作也可能被回滚成"半完成"状态。</li>
                </ul>
                <p>修复方案：把 DELETE + INSERT 包进一个显式事务，失败时 rollback。</p>
              </div>

              <div class="tool-card">
                <div class="tool-head" onclick="toggleTool(this)"><span class="tico">✏️</span><span class="tname">edit_file</span><span class="targ">core/memory_compressor.py</span><span class="badge ok">✓ 已修改</span><span class="badge" style="background:rgba(63,185,80,.12);color:var(--green)">+9 −2</span></div>
                <div class="tool-body">@@ -102,8 +102,15 @@
      summary = self._summarize(rows)
      cur = self.conn.cursor()
-     cur.execute("DELETE FROM messages WHERE session_id=? AND compressed=0", (session_id,))
-     cur.execute("INSERT INTO summaries ...")
+     try:
+         cur.execute("BEGIN")
+         cur.execute("INSERT INTO summaries ...")
+         cur.execute("DELETE FROM messages WHERE session_id=? AND compressed=0", (session_id,))
+         self.conn.commit()
+     except Exception:
+         self.conn.rollback()
+         raise</div>
              </div>

              <div class="tool-card">
                <div class="tool-head" onclick="toggleTool(this)"><span class="tico">🖥</span><span class="tname">shell</span><span class="targ">venv\Scripts\python -m pytest tests/test_memory.py -q</span><span class="badge ok">✓ 退出码 0</span></div>
                <div class="tool-body">.....................                                   [100%]
21 passed in 1.87s</div>
              </div>

              <div class="md-body"><p>已修复并通过全部 21 个测试。核心改动：INSERT summary 先于 DELETE，且包在同一个事务里，任何一步失败都会整体回滚，不会再出现"删了但没存摘要"的丢消息窗口。</p></div>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- 二级页面（技能 / MCP） -->
    <div id="subpage">
      <div class="sub-head">
        <button class="sub-back" id="sub-back">← 返回</button>
        <h2 id="sub-title">技能 Skills</h2>
        <span class="sub-desc" id="sub-desc"></span>
      </div>
      <div class="sub-body" id="sub-body"></div>
    </div>

    <!-- 输入区 -->
    <div id="input-area">
      <div id="input-wrap">
        <div id="input-box">
          <textarea id="msg-input" rows="2" placeholder="向星瑶提问… 输入 / 查看命令，@ 引用文件"></textarea>
          <div id="input-bar">
            <button class="chip-btn" id="btn-attach" title="附加文件">📎 文件</button>
            <button class="chip-btn" id="btn-think" title="深度思考">🧠 思考</button>
            <button class="chip-btn" id="btn-plan" title="计划模式">📋 计划</button>
            <button class="chip-btn" id="btn-cmd" title="斜杠命令">/ 命令</button>
            <button id="send-btn">发送 ⏎</button>
          </div>
        </div>
      </div>
      <div id="input-meta">
        <div class="l"><span id="token-usage">Token: 12,480 / 128,000</span><span>上下文占用 9.8%</span></div>
        <div class="r"><span class="kbd">Enter</span> 发送 · <span class="kbd">Shift+Enter</span> 换行 · <span class="kbd">Esc</span> 中断</div>
      </div>
    </div>
  </div>

  <!-- ===== 右侧栏 ===== -->
  <div id="rightbar">
    <div id="right-tabs">
      <button class="rtab active" data-pane="plan">📋 计划</button>
      <button class="rtab" data-pane="tasks">⏵ 任务 <span id="task-count" style="background:var(--accent);color:#fff;border-radius:8px;padding:0 6px;font-size:10.5px">2</span></button>
      <button class="rtab" data-pane="terminal">🖥 终端</button>
      <button class="rtab" data-pane="files">📁 文件</button>
    </div>
    <div id="right-content">

      <div class="rpane active" id="pane-plan">
        <div class="progress-line"><span>修复 memory 压缩器</span><span id="plan-pct">60%</span></div>
        <div class="progress-bar"><i id="plan-bar" style="width:60%"></i></div>
        <div class="plan-item done"><div class="pstat">✓</div><div><div class="pt">读取 memory_compressor.py 定位删除路径</div><div class="pm">stage=plan · explore</div></div></div>
        <div class="plan-item done"><div class="pstat">✓</div><div><div class="pt">grep 全库 compressed 标记的使用点</div><div class="pm">stage=plan · explore</div></div></div>
        <div class="plan-item done"><div class="pstat">✓</div><div><div class="pt">实现事务化修复 + 调整写入顺序</div><div class="pm">stage=build · implement</div></div></div>
        <div class="plan-item doing" id="plan-doing"><div class="pstat"><span class="spin"></span></div><div><div class="pt">运行回归测试 test_memory.py</div><div class="pm">stage=build · test · verification=command</div></div></div>
        <div class="plan-item todo"><div class="pstat"></div><div><div class="pt">审查 diff 并输出总结</div><div class="pm">stage=review · review</div></div></div>
      </div>

      <div class="rpane" id="pane-tasks">
        <div class="bg-task" data-running="1">
          <div class="bth" onclick="this.parentElement.classList.toggle('open')"><span class="spin"></span><span class="mono">pytest --watch tests/</span><button class="bt-kill" onclick="event.stopPropagation();killTask(this)">终止</button></div>
          <div class="btlog">================ test session starts ================
collected 87 items
tests/test_memory.py .......         [ 12%]
tests/test_llm.py ..........          [ 24%]
tests/test_cancellation.py ...        [ 30%]
watching for changes...</div>
        </div>
        <div class="bg-task" data-running="1">
          <div class="bth" onclick="this.parentElement.classList.toggle('open')"><span class="spin"></span><span class="mono">python main.py (调试实例)</span><button class="bt-kill" onclick="event.stopPropagation();killTask(this)">终止</button></div>
          <div class="btlog">[11:02:14] 加载配置 config.json
[11:02:14] 连接 SQLite ycode.db ✓
[11:02:15] 注册 14 个内置工具
[11:02:15] MCP weather-server 握手成功
[11:02:15] TUI 已启动</div>
        </div>
        <div class="bg-task" data-running="0">
          <div class="bth" onclick="this.parentElement.classList.toggle('open')"><span style="color:var(--green)">✓</span><span class="mono">build_release.py</span><span class="btd">已退出 · 0</span></div>
          <div class="btlog">PyInstaller 打包完成
dist/ycode.exe  (28.4 MB)
dist/ycode_setup.exe
dist/ycode_uninstall.exe</div>
        </div>
      </div>

      <div class="rpane" id="pane-terminal">
        <div class="term-body">
          <div class="t-out"><span class="t-cmd">$ git status --short</span>
 M core/memory_compressor.py
 M tests/test_memory.py
?? demo/
<span class="t-cmd">$ venv\Scripts\python -m pytest tests/test_memory.py -q</span>
<span class="t-ok">21 passed in 1.87s</span>
<span class="t-warn">⚠ DeprecationWarning: litellm 1.x 将在下个主版本移除 set_verbose</span></div>
          <div class="term-input-row">$&nbsp;<input id="term-input" placeholder="输入命令，回车执行"></div>
        </div>
      </div>

      <div class="rpane" id="pane-files">
        <div id="file-tree"></div>
      </div>

    </div>
  </div>
</div>

<!-- ===== 状态栏 ===== -->
<div id="statusbar">
  <span class="si"><span class="s-dot"></span>模型已连接</span>
  <span class="si">⚙ 权限: <span id="sb-mode">默认模式</span></span>
  <span class="si">🧰 工具 14 · MCP 2</span>
  <div class="right">
    <span class="si" id="sb-turn">空闲</span>
    <span class="si">UTF-8</span>
    <span class="si">Ln 12, Col 4</span>
  </div>
</div>

<!-- ===== 权限确认弹窗 ===== -->
<div class="modal-mask" id="perm-modal">
  <div class="modal">
    <div class="modal-head">
      <div class="mh-ico">⚠️</div>
      <div><h3>权限请求 · <span id="perm-tool">shell</span></h3><p>星瑶请求执行以下操作，当前为默认权限模式</p></div>
    </div>
    <div class="modal-body" id="perm-cmd"></div>
    <div class="modal-foot">
      <button class="btn" id="perm-always">总是允许此类操作</button>
      <button class="btn danger" id="perm-deny">拒绝</button>
      <button class="btn primary" id="perm-allow">允许一次 ⏎</button>
    </div>
  </div>
</div>

<!-- 通用下拉 -->
<div id="dropdown"></div>
<!-- Toast -->
<div id="toast"></div>

<script>
/* ================= 基础交互 ================= */
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const chatScroll = $('#chat-scroll');
function scrollBottom(){ chatScroll.scrollTop = chatScroll.scrollHeight; }
function toggleTool(head){ head.parentElement.classList.toggle('open'); }

/* Toast */
let toastTimer;
function toast(msg, type){
  const t = $('#toast');
  t.textContent = msg; t.className = 'show ' + (type||'');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.className='', 2200);
}

/* 侧栏开关 */
$('#btn-toggle-side').onclick = () => $('#sidebar').classList.toggle('collapsed');
$('#btn-toggle-right').onclick = () => $('#rightbar').classList.toggle('collapsed');

/* 主题切换 */
$('#btn-theme').onclick = function(){
  document.body.classList.toggle('light');
  this.textContent = document.body.classList.contains('light') ? '☀️' : '🌙';
  toast('已切换主题');
};

/* 右侧 Tab */
$$('.rtab').forEach(tab => tab.onclick = () => {
  $$('.rtab').forEach(t=>t.classList.remove('active'));
  $$('.rpane').forEach(p=>p.classList.remove('active'));
  tab.classList.add('active');
  $('#pane-'+tab.dataset.pane).classList.add('active');
});

/* 会话切换 */
$('#session-list').addEventListener('click', e => {
  const fbtn = e.target.closest('.f-btn');
  if(fbtn) return;
  const frow = e.target.closest('.folder-row');
  if(frow){
    const folder = frow.parentElement;
    const kids = folder.querySelector('.folder-children');
    const open = kids.style.display !== 'none';
    kids.style.display = open ? 'none' : 'block';
    frow.querySelector('.f-arrow').textContent = open ? '▸' : '▾';
    return;
  }
  const s = e.target.closest('.session-item');
  if(s){
    $$('.session-item').forEach(x=>x.classList.remove('active'));
    s.classList.add('active');
    $('#crumb-title').textContent = s.dataset.title;
    toast('已切换到会话: ' + s.dataset.title);
  }
});

/* 文件夹操作 */
$('#session-list').addEventListener('click', e => {
  const fbtn = e.target.closest('.f-btn');
  if(!fbtn) return;
  e.stopPropagation();
  const folder = fbtn.closest('.folder-item');
  const nameEl = folder.querySelector('.f-name');
  const act = fbtn.dataset.act;
  if(act === 'new'){
    createSession(folder.querySelector('.folder-children'));
    folder.querySelector('.folder-children').style.display = 'block';
    folder.querySelector('.f-arrow').textContent = '▾';
  } else if(act === 'rename'){
    const n = prompt('重命名文件夹', nameEl.textContent);
    if(n && n.trim()){ nameEl.textContent = n.trim(); toast('已重命名为: ' + n.trim(), 'ok'); }
  } else if(act === 'del'){
    if(confirm('删除文件夹 "' + nameEl.textContent + '" 及其中的会话?')){
      folder.remove(); toast('文件夹已删除', 'warn');
    }
  }
});

function createSession(container){
  const el = document.createElement('div');
  el.className = 'session-item active';
  el.dataset.title = '新会话';
  el.innerHTML = '<div class="t">新会话</div><div class="m">刚刚 · 0 条消息</div>';
  $$('.session-item').forEach(x=>x.classList.remove('active'));
  container.prepend(el);
  $('#crumb-title').textContent = '新会话';
  toast('已创建新会话', 'ok');
}

$('#btn-new-session').onclick = () => createSession($('#session-list'));

let folderSeq = 1;
$('#btn-new-folder').onclick = () => {
  const name = prompt('文件夹名称', '新建文件夹 ' + folderSeq++);
  if(!name || !name.trim()) return;
  const el = document.createElement('div');
  el.className = 'folder-item';
  el.innerHTML = `<div class="folder-row">
      <span class="f-arrow">▾</span><span class="f-ico">🗀</span><span class="f-name"></span>
      <span class="f-actions"><button class="f-btn" data-act="new" title="在此文件夹新建会话">＋</button><button class="f-btn" data-act="rename" title="重命名">✎</button><button class="f-btn" data-act="del" title="删除文件夹">✕</button></span>
    </div><div class="folder-children"></div>`;
  el.querySelector('.f-name').textContent = name.trim();
  $('#session-list').prepend(el);
  toast('已创建文件夹: ' + name.trim(), 'ok');
};

$('#btn-clear').onclick = () => { $('#chat-body').innerHTML=''; toast('会话已清空'); };
$('#btn-export').onclick = () => toast('会话已导出为 Markdown', 'ok');
$('#btn-settings').onclick = () => toast('设置面板');

function killTask(btn){
  const task = btn.closest('.bg-task');
  task.dataset.running = '0';
  const bth = task.querySelector('.bth');
  bth.querySelector('.spin')?.remove();
  btn.remove();
  bth.insertAdjacentHTML('afterbegin','<span style="color:var(--red)">✕</span>');
  const c = $('#task-count'); c.textContent = Math.max(0, parseInt(c.textContent)-1);
  toast('任务已终止', 'warn');
}

/* 下拉菜单 */
const dd = $('#dropdown');
function showDropdown(anchor, items){
  dd.innerHTML = items.map((it,i)=> it==='-' ? '<div class="dd-sep"></div>' :
    `<div class="dd-item" data-i="${i}">${it.icon||''} ${it.label}${it.checked?'<span class="ck">✓</span>':''}</div>`).join('');
  const r = anchor.getBoundingClientRect();
  dd.style.left = Math.min(r.left, innerWidth-220)+'px';
  dd.style.top = (r.bottom+6)+'px';
  dd.classList.add('show');
  dd.querySelectorAll('.dd-item').forEach(el => el.onclick = () => {
    dd.classList.remove('show');
    items[+el.dataset.i].action && items[+el.dataset.i].action();
  });
}
document.addEventListener('click', e => { if(!dd.contains(e.target)) dd.classList.remove('show'); });

const models = ['agnes-2.0-flash','agnes-2.0-pro','gpt-5-codex','claude-sonnet-4.5','deepseek-v3.2'];
$('#btn-model').onclick = e => {
  e.stopPropagation();
  showDropdown($('#btn-model'), models.map(m => ({
    label: m, icon:'🤖', checked: $('#model-name').textContent===m,
    action(){
      $('#model-name').textContent=m;
      if(window.pywebview && window.pywebview.api) window.pywebview.api.set_model(m);
      toast('已切换模型: '+m, 'ok');
    }
  })));
};

const permModes = [{n:'默认模式',c:'var(--yellow)'},{n:'自动接受编辑',c:'var(--cyan)'},{n:'计划模式',c:'var(--purple)'},{n:'YOLO (全部允许)',c:'var(--red)'}];
$('#btn-mode').onclick = e => {
  e.stopPropagation();
  showDropdown($('#btn-mode'), permModes.map(m => ({
    label: m.n, icon:'🛡', checked: $('#mode-name').textContent===m.n,
    action(){
      $('#mode-name').textContent=m.n; $('#sb-mode').textContent=m.n;
      $('#mode-dot').style.background=m.c; $('#mode-dot').style.boxShadow='0 0 6px '+m.c;
      if(window.pywebview && window.pywebview.api) window.pywebview.api.set_permission_mode(m.n);
      toast('权限模式: '+m.n, m.n.includes('YOLO')?'warn':'');
    }
  })));
};

$('#btn-attach').onclick = () => toast('选择文件附加');
$('#btn-think').onclick = function(){ this.classList.toggle('on'); toast(this.classList.contains('on')?'深度思考已开启':'深度思考已关闭'); };
$('#btn-plan').onclick = function(){ this.classList.toggle('on'); toast(this.classList.contains('on')?'计划模式已开启':'计划模式已关闭'); };
$('#btn-cmd').onclick = e => {
  e.stopPropagation();
  const cmds = ['/help 显示帮助','/plan 管理计划','/model 切换模型','/skill 管理技能','/memory 查看记忆','/compact 压缩上下文','/tasks 后台任务','/quit 退出'];
  showDropdown($('#btn-cmd'), cmds.map(c => ({ label:c, action(){ $('#msg-input').value = c.split(' ')[0]+' '; $('#msg-input').focus(); } })));
};

const fileTree = [
  {n:'core', d:1, children:[{n:'memory_compressor.py'},{n:'memory.py'},{n:'llm.py'},{n:'turn_renderer.py'},{n:'plan.py'}]},
  {n:'tests', d:1, children:[{n:'test_memory.py'},{n:'test_llm.py'}]},
  {n:'skills', d:1, children:[{n:'hello_skill.py'}]},
  {n:'main.py'},{n:'config.py'},{n:'config.json'},{n:'system_prompt.md'},{n:'AGENTS.md'}
];
function buildTree(nodes, container){
  container.innerHTML = '';
  nodes.forEach(node => {
    const row = document.createElement('div');
    row.className = 'file-row';
    row.innerHTML = node.d ? `<span class="fi">▸</span>📂 ${node.n}` : `<span class="fi"></span>📄 ${node.n}`;
    container.appendChild(row);
    if(node.d){
      const kids = document.createElement('div');
      kids.className = 'file-children';
      if(node.children) buildTree(node.children, kids);
      container.appendChild(kids);
      row.onclick = () => { row.classList.toggle('open'); row.querySelector('.fi').textContent = row.classList.contains('open')?'▾':'▸'; };
    } else {
      row.onclick = () => toast('打开文件: ' + node.n);
    }
  });
}
buildTree(fileTree, $('#file-tree'));

/* 终端 Shell 执行 */
$('#term-input').addEventListener('keydown', async function(e){
  if(e.key !== 'Enter' || !this.value.trim()) return;
  const cmd = this.value.trim(); this.value = '';
  const out = this.closest('.term-body').querySelector('.t-out');
  if(cmd==='clear'){ out.innerHTML=''; return; }
  out.innerHTML += '<span class="t-cmd">$ '+cmd.replace(/</g,'&lt;')+'</span>\n';
  
  if(window.pywebview && window.pywebview.api){
    try {
      const res = await window.pywebview.api.execute_terminal_cmd(cmd);
      if(res.stdout) out.innerHTML += res.stdout.replace(/</g,'&lt;') + '\n';
      if(res.stderr) out.innerHTML += '<span class="t-warn">' + res.stderr.replace(/</g,'&lt;') + '</span>\n';
    } catch(err) {
      out.innerHTML += '<span class="t-warn">执行出错: ' + err + '</span>\n';
    }
  } else {
    let resp = 'bash: '+cmd+': command not found';
    if(cmd==='ls'||cmd==='dir') resp = 'core/  tests/  demo/  main.py  config.py  ycode.db';
    if(cmd==='git status') resp = 'On branch main\nYour branch is up to date.';
    out.innerHTML += resp + '\n';
  }
  this.closest('.term-body').scrollTop = 1e9;
});

/* ================= PyWebView 桥接与对话 ================= */
const input = $('#msg-input');
const sendBtn = $('#send-btn');
let running = false, cancelFlag = false, tokens = 12480;
let currentTurnArea = null;
let currentThinkingBlock = null;
let activeTurnTimer = null;
let currentReqId = null;

input.addEventListener('keydown', e => {
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); sendBtn.click(); }
});

sendBtn.onclick = () => {
  if(running){
    if(window.pywebview && window.pywebview.api){
      window.pywebview.api.stop_current_turn();
    } else {
      cancelFlag = true;
    }
    toast('已请求中断当前回合 (Esc)', 'warn');
    return;
  }
  const text = input.value.trim();
  if(!text) return;
  input.value = '';
  
  if(window.pywebview && window.pywebview.api){
    window.pywebview.api.send_user_message(text);
  } else {
    runTurn(text);
  }
};

document.addEventListener('keydown', e => {
  if(e.key==='Escape'){
    if($('#perm-modal').classList.contains('show')) denyPerm();
    else if(running){
      if(window.pywebview && window.pywebview.api) window.pywebview.api.stop_current_turn();
      else cancelFlag = true;
      toast('已请求中断当前回合', 'warn');
    }
  }
});

function addUserMsg(text){
  const el = document.createElement('div');
  el.className = 'msg msg-user';
  el.innerHTML = '<div class="bubble"></div>';
  el.querySelector('.bubble').textContent = text;
  $('#chat-body').appendChild(el);
  scrollBottom();
}

function newAssistantMsg(){
  const el = document.createElement('div');
  el.className = 'msg msg-assistant';
  el.innerHTML = `<div class="avatar-row"><div class="avatar">✦</div>
    <div class="assistant-body">
      <div class="assistant-name"><b>星瑶</b> · ${$('#model-name').textContent} · <span class="elapsed">0.0s</span></div>
      <div class="stream-area"></div>
    </div></div>`;
  $('#chat-body').appendChild(el);
  scrollBottom();
  return el.querySelector('.stream-area');
}

function bumpTokens(n){
  tokens += n;
  $('#token-usage').textContent = 'Token: ' + tokens.toLocaleString() + ' / 128,000';
}

/* 原生 PyWebView 事件回调 */
window.onBridgeEvent = function(event){
  const type = event.type;
  const data = event.data;

  if(type === "turn_start"){
    running = true;
    sendBtn.classList.add('stop');
    sendBtn.innerHTML = '■ 停止 (Esc)';
    $('#sb-turn').textContent = '生成中…';
    addUserMsg(data.user_text);
    currentTurnArea = newAssistantMsg();
    const t0 = Date.now();
    activeTurnTimer = setInterval(() => {
      const el = currentTurnArea.closest('.assistant-body').querySelector('.elapsed');
      if(el) el.textContent = ((Date.now()-t0)/1000).toFixed(1)+'s';
    }, 100);
  }
  else if(type === "thinking_delta"){
    if(!currentThinkingBlock){
      const el = document.createElement('div');
      el.className = 'think-block';
      el.innerHTML = `<div class="tk">💭 思考过程 <span style="margin-left:auto">▾</span></div><div class="tk-cnt" style="display:block"></div>`;
      el.querySelector('.tk').onclick = function(){
        const c = this.nextElementSibling; c.style.display = c.style.display==='none' ? 'block' : 'none';
      };
      currentThinkingBlock = el.querySelector('.tk-cnt');
      currentTurnArea.appendChild(el);
    }
    currentThinkingBlock.textContent += data.text;
    scrollBottom();
  }
  else if(type === "token_delta"){
    let p = currentTurnArea.querySelector('.md-body.live');
    if(!p){
      p = document.createElement('div');
      p.className = 'md-body live';
      p.innerHTML = '<p><span class="txt"></span><span class="cursor-blink"></span></p>';
      currentTurnArea.appendChild(p);
    }
    p.querySelector('.txt').textContent += data.text;
    bumpTokens(1);
    scrollBottom();
  }
  else if(type === "tool_start"){
    const card = document.createElement('div');
    card.className = 'tool-card';
    card.id = 'tool-' + (data.id || Math.random().toString(36).slice(2, 8));
    card.innerHTML = `<div class="tool-head" onclick="toggleTool(this)"><span class="tico">🔧</span><span class="tname">${data.tool_name||'tool'}</span>
      <span class="targ">${JSON.stringify(data.args||{})}</span><span class="badge run"><span class="spin"></span> 运行中</span></div>
      <div class="tool-body">正在执行操作...</div>`;
    currentTurnArea.appendChild(card);
    scrollBottom();
  }
  else if(type === "tool_finish"){
    const card = $('#tool-' + data.id);
    if(card){
      const badge = card.querySelector('.badge');
      badge.className = 'badge ' + (data.error ? 'err' : 'ok');
      badge.innerHTML = data.error ? '✕ 出错' : '✓ 完成';
      card.querySelector('.tool-body').textContent = data.result || data.error || '完成';
    }
  }
  else if(type === "permission_request"){
    currentReqId = data.request_id;
    openPerm(data.tool_name, JSON.stringify(data.details, null, 2),
      () => window.pywebview.api.respond_permission(currentReqId, true, false),
      () => window.pywebview.api.respond_permission(currentReqId, false, false)
    );
  }
  else if(type === "turn_finish" || type === "turn_idle"){
    if(currentTurnArea){
      currentTurnArea.querySelector('.cursor-blink')?.remove();
    }
    clearInterval(activeTurnTimer);
    running = false;
    currentThinkingBlock = null;
    sendBtn.classList.remove('stop');
    sendBtn.innerHTML = '发送 ⏎';
    $('#sb-turn').textContent = '空闲';
    scrollBottom();
  }
  else if(type === "error"){
    toast(data.message || "运行错误", "warn");
  }
};

/* 权限弹窗 */
let permAllowCb, permDenyCb;
function openPerm(tool, cmd, onAllow, onDeny){
  $('#perm-tool').textContent = tool;
  $('#perm-cmd').textContent = cmd;
  $('#perm-modal').classList.add('show');
  permAllowCb = onAllow; permDenyCb = onDeny;
}
$('#perm-allow').onclick = () => { $('#perm-modal').classList.remove('show'); permAllowCb && permAllowCb(); };
$('#perm-always').onclick = () => {
  $('#perm-modal').classList.remove('show');
  toast('已将此类操作加入允许列表', 'ok');
  if(window.pywebview && window.pywebview.api && currentReqId){
    window.pywebview.api.respond_permission(currentReqId, true, true);
  } else {
    permAllowCb && permAllowCb();
  }
};
function denyPerm(){ $('#perm-modal').classList.remove('show'); permDenyCb && permDenyCb(); }
$('#perm-deny').onclick = denyPerm;

function advancePlan(){
  const doing = $('#plan-doing');
  if(doing && doing.classList.contains('doing')){
    doing.classList.remove('doing'); doing.classList.add('done');
    doing.querySelector('.pstat').innerHTML = '✓';
    const next = doing.nextElementSibling;
    if(next && next.classList.contains('todo')){
      next.classList.remove('todo'); next.classList.add('doing');
      next.querySelector('.pstat').innerHTML = '<span class="spin"></span>';
    }
  }
  const items = $$('#pane-plan .plan-item');
  const done = [...items].filter(i=>i.classList.contains('done')).length;
  const pct = Math.round(done/items.length*100);
  $('#plan-bar').style.width = pct+'%';
  $('#plan-pct').textContent = pct+'%';
}

/* 演示回合 */
function streamText(area, text){
  return new Promise(resolve => {
    const p = document.createElement('div');
    p.className = 'md-body';
    p.innerHTML = '<p><span class="txt"></span><span class="cursor-blink"></span></p>';
    area.appendChild(p);
    const span = p.querySelector('.txt');
    let i = 0;
    const timer = setInterval(() => {
      if(cancelFlag){ clearInterval(timer); p.querySelector('.cursor-blink')?.remove(); resolve(false); return; }
      i += 2 + Math.floor(Math.random()*3);
      span.textContent = text.slice(0, i);
      bumpTokens(2);
      scrollBottom();
      if(i >= text.length){
        clearInterval(timer);
        p.querySelector('.cursor-blink')?.remove();
        resolve(true);
      }
    }, 28);
  });
}

function addThink(area, text){
  const el = document.createElement('div');
  el.className = 'think-block';
  el.innerHTML = `<div class="tk">💭 思考过程 · ${(1+Math.random()*3).toFixed(1)}s <span style="margin-left:auto">▾</span></div>
    <div class="tk-cnt" style="display:none"></div>`;
  el.querySelector('.tk-cnt').textContent = text;
  el.querySelector('.tk').onclick = function(){
    const c = this.nextElementSibling;
    c.style.display = c.style.display==='none' ? 'block' : 'none';
  };
  area.appendChild(el);
  scrollBottom();
}

function makeToolCard(area, {ico, name, arg, body, needsPerm=false, permText=''}){
  return new Promise(resolve => {
    const card = document.createElement('div');
    card.className = 'tool-card';
    card.innerHTML = `<div class="tool-head"><span class="tico">${ico}</span><span class="tname">${name}</span>
      <span class="targ">${arg}</span><span class="badge run"><span class="spin"></span> 运行中</span></div>
      <div class="tool-body">${body}</div>`;
    card.querySelector('.tool-head').onclick = () => card.classList.toggle('open');
    area.appendChild(card);
    scrollBottom();
    $('#sb-turn').textContent = '工具: ' + name;

    const finish = (ok) => {
      const badge = card.querySelector('.badge');
      badge.className = 'badge ' + (ok==='denied' ? 'err' : 'ok');
      badge.innerHTML = ok==='denied' ? '✕ 被拒绝' : '✓ 完成';
      if(ok==='denied') card.querySelector('.tool-body').innerHTML = '<span style="color:var(--red)">用户拒绝了此操作，已跳过。</span>';
      resolve(ok);
    };

    if(needsPerm){
      openPerm(name, permText,
        () => setTimeout(()=>finish(true), 700),
        () => finish('denied'));
    } else {
      setTimeout(()=>finish(true), 900 + Math.random()*600);
    }
  });
}

async function runTurn(userText){
  running = true; cancelFlag = false;
  sendBtn.classList.add('stop');
  sendBtn.innerHTML = '■ 停止 (Esc)';
  $('#sb-turn').textContent = '生成中…';
  addUserMsg(userText);
  const area = newAssistantMsg();
  const t0 = Date.now();
  const timer = setInterval(() => {
    const el = area.closest('.assistant-body').querySelector('.elapsed');
    if(el) el.textContent = ((Date.now()-t0)/1000).toFixed(1)+'s';
  }, 100);

  addThink(area, `用户请求: "${userText.slice(0,40)}${userText.length>40?'…':''}"
1. 先理解目标与验收条件
2. 检索相关代码位置
3. 最小化修改并验证`);

  const ok1 = await streamText(area, '收到，我先检索相关代码，确认改动范围。');
  if(ok1 && !cancelFlag){
    const r = await makeToolCard(area, {
      ico:'🔍', name:'grep', arg:`"${userText.split(/\s+/)[0]}" core/ tests/`,
      body:'core/llm.py:88\ncore/turn_controller.py:143\ntests/test_llm.py:57\n<span class="ln-dim">共 3 处匹配</span>'
    });
    if(!cancelFlag && r===true){
      const r2 = await makeToolCard(area, {
        ico:'🖥', name:'shell', arg:'venv\\Scripts\\python -m pytest tests/ -q',
        body:'......................................                       [100%]\n<span style="color:var(--green)">38 passed in 2.41s</span>',
        needsPerm: $('#mode-name').textContent==='默认模式',
        permText:'venv\\Scripts\\python -m pytest tests/ -q\n\n工作目录: E:\\fch\\Y-code_tui\n风险等级: 低 (只读测试命令)'
      });
      if(!cancelFlag && r2===true){
        await streamText(area, `分析完成。根据检索结果，与你请求相关的逻辑集中在 core/llm.py 和 turn_controller.py，全部 38 个测试通过。`);
        advancePlan();
      }
    }
  }
  clearInterval(timer);
  running = false;
  sendBtn.classList.remove('stop');
  sendBtn.innerHTML = '发送 ⏎';
  $('#sb-turn').textContent = '空闲';
  scrollBottom();
}

/* 初始化原生 PyWebView 数据 */
window.addEventListener('pywebviewready', async () => {
  if(!window.pywebview || !window.pywebview.api) return;
  try {
    const init = await window.pywebview.api.get_initial_state();
    if(init.current_model) $('#model-name').textContent = init.current_model;
    if(init.permission_mode) {
      $('#mode-name').textContent = init.permission_mode;
      $('#sb-mode').textContent = init.permission_mode;
    }
    if(init.file_tree && init.file_tree.length > 0){
      buildTree(init.file_tree, $('#file-tree'));
    }
    toast('GUI 后端桥接连接成功！', 'ok');
  } catch(e) {
    console.error("Failed to load initial GUI state:", e);
  }
});

/* 二级页面：技能与 MCP */
const skillsData = [
  {name:'hello_skill', ico:'⚡', scope:'项目', desc:'项目内示例技能，演示技能注册、激活与调用流程。', on:true},
  {name:'systematic-debugging', ico:'🧠', scope:'用户', desc:'遇到 bug、测试失败或异常行为时使用，在提出修复前先系统化定位根因。', on:true},
  {name:'writing-plans', ico:'📝', scope:'用户', desc:'多步任务在动手前先编写实施计划，包含依赖、验证与回滚步骤。', on:true}
];
const mcpData = [
  {name:'weather-server', ico:'🌤', desc:'天气查询 MCP 服务，通过 get_weather 提供实时天气。', on:true,
   tools:[['get_weather','查询指定城市天气'],['get_forecast','未来 3 天预报']]},
  {name:'filesystem', ico:'🗂', desc:'受限目录文件系统访问，提供读写与列目录能力。', on:true,
   tools:[['fs_read','读取文件'],['fs_write','写入文件']]}
];

function openSubpage(type){
  const body = $('#sub-body');
  if(type === 'skills'){
    $('#sub-title').textContent = '技能 Skills';
    $('#sub-desc').textContent = '管理已安装的技能，开关控制是否注入系统提示词';
    body.innerHTML = '<div class="sub-add"><button class="btn" id="sub-add-skill">＋ 安装新技能</button></div>' +
      '<div class="sub-grid">' + skillsData.map((s,i) => `
      <div class="sub-card" data-i="${i}">
        <div class="sc-top"><div class="sc-ico">${s.ico}</div><div class="sc-name">${s.name}</div><span class="sc-scope">${s.scope}</span></div>
        <div class="sc-desc">${s.desc}</div>
        <div class="sc-foot"><span>${s.on ? '已启用 · 每轮注入' : '已停用'}</span><div class="switch ${s.on?'on':''}" data-i="${i}"></div></div>
      </div>`).join('') + '</div>';
    body.querySelectorAll('.switch').forEach(sw => sw.onclick = e => {
      e.stopPropagation();
      const i = +sw.dataset.i;
      skillsData[i].on = !skillsData[i].on;
      sw.classList.toggle('on');
      toast((skillsData[i].on?'已启用技能: ':'已停用技能: ') + skillsData[i].name, skillsData[i].on?'ok':'warn');
    });
  } else {
    $('#sub-title').textContent = 'MCP 服务';
    $('#sub-desc').textContent = '管理 mcp.json 中配置的外部工具服务';
    body.innerHTML = '<div class="sub-add"><button class="btn" id="sub-add-mcp">＋ 添加 MCP 服务</button></div>' +
      '<div class="sub-grid">' + mcpData.map((m,i) => `
      <div class="sub-card mcp-card" data-i="${i}">
        <div class="sc-top"><div class="sc-ico">${m.ico}</div><div class="sc-name">${m.name}</div>
          <span class="mcp-status ${m.on?'':'off'}" style="margin-left:auto"><span class="st"></span>${m.on?'已连接':'离线'}</span></div>
        <div class="sc-desc">${m.desc}</div>
        <div class="mcp-tools">${m.tools.map(t => `<div class="t-tool">🔧 ${t[0]}<span class="t-desc">${t[1]}</span></div>`).join('')}</div>
      </div>`).join('') + '</div>';
  }
  $('#subpage').classList.add('show');
}
$('#menu-skills').onclick = () => openSubpage('skills');
$('#menu-mcp').onclick = () => openSubpage('mcp');
$('#sub-back').onclick = () => $('#subpage').classList.remove('show');
document.addEventListener('keydown', e => {
  if(e.key === 'Escape') $('#subpage').classList.remove('show');
});

input.focus();
</script>
</body>
</html>""")
print("Re-written demo/ycode_gui_demo.html in clean UTF-8!")
