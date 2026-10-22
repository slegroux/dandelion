// Terminal-style history of sent Ask-AI prompts (pure logic, no DOM). composer.js
// binds Up/Down to older()/newer(). Entries persist in `storage` (localStorage in
// the page); if storage throws, the history still works for this page load.
window.AskHistory = function(storage){
  var KEY = 'dandelion.askHistory', MAX = 200;
  var entries = [];
  var idx = -1;          // -1 = not browsing; else index into entries
  var draft = '';        // what was being typed when browsing started
  function load(){
    try {
      var v = JSON.parse(storage.getItem(KEY));
      if(Array.isArray(v)) entries = v.filter(function(s){ return typeof s === 'string'; }).slice(-MAX);
    } catch(e){}         // keep the in-memory entries
  }
  function save(){ try { storage.setItem(KEY, JSON.stringify(entries)); } catch(e){} }
  load();
  return {
    push: function(text){
      idx = -1;
      text = String(text == null ? '' : text);
      if(!text.trim() || entries[entries.length - 1] === text) return;
      entries.push(text);
      if(entries.length > MAX) entries = entries.slice(-MAX);
      save();
    },
    older: function(current){
      if(idx === -1){
        load();          // pick up prompts sent from other tabs/dialogs
        if(!entries.length) return null;
        draft = current;
        idx = entries.length - 1;
      } else if(idx > 0){ idx--; }
      else return null;
      return entries[idx];
    },
    newer: function(){
      if(idx === -1) return null;
      if(idx === entries.length - 1){ idx = -1; return draft; }
      idx++;
      return entries[idx];
    },
    reset: function(){ idx = -1; },
    // Recall right away (no caret check)? Empty text, or still the unedited entry
    // last recalled — so walking through history is one press per entry.
    immediate: function(current){
      return current === '' || (idx !== -1 && current === entries[idx]);
    }
  };
};
