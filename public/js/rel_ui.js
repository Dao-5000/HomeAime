/* ============================================================
   亲密度/关系数值界面开关（2026-09-15）
   ------------------------------------------------------------
   背景：用户拍板砍掉亲密度自动成长（后端 config.RELATIONSHIP_AUTO_UPDATE=false），
   并把这块的数值面板隐藏（RELATIONSHIP_UI_VISIBLE=false）——数值不再变化、还可能
   显示成被重置的旧值，看着只会添堵。

   用法（各渲染处）：
     window.relUIShow()  -> true 才渲染亲密度/好感/信任/阶段 这类数字
   默认 false（先按隐藏渲染），等 /api/pc/config 回来若为 true 再重渲染一次，
   这样不会出现"先闪一下数字再消失"。
   ============================================================ */
window.RelUI = window.RelUI || { visible: false, ready: false, _cbs: [] };

/** 是否展示关系数值面板（未就绪时按"隐藏"处理） */
window.relUIShow = function () {
  try { return !!(window.RelUI && window.RelUI.visible); } catch (e) { return false; }
};

/** 配置就绪后回调（已就绪则立即执行） */
window.relUIReady = function (cb) {
  try {
    if (window.RelUI.ready) { cb(window.RelUI.visible); }
    else { window.RelUI._cbs.push(cb); }
  } catch (e) {}
};

(function () {
  function _apply(v) {
    window.RelUI.visible = !!v;
    window.RelUI.ready = true;
    var cbs = window.RelUI._cbs || [];
    window.RelUI._cbs = [];
    for (var i = 0; i < cbs.length; i++) { try { cbs[i](window.RelUI.visible); } catch (e) {} }
    // 通知各页面重渲染（总览/联系人/人格页都会读这个标志）
    try { window.hzRenderOverview && window.hzRenderOverview(); } catch (e) {}
    try { window.renderContacts && window.renderContacts(); } catch (e) {}
  }
  try {
    fetch('/api/pc/config').then(function (r) { return r.json(); }).then(function (cfg) {
      var v = cfg ? cfg.RELATIONSHIP_UI_VISIBLE : false;
      _apply(v === true || v === 'true' || v === 1);
    }).catch(function () { _apply(false); });
  } catch (e) { _apply(false); }
})();
