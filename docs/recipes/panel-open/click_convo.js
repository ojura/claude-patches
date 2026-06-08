(function(){
  var needle=NEEDLE, rows=[...document.querySelectorAll('div,li,a,button')], target=null;
  for(var e of rows){var t=(e.innerText||'').replace(/\s+/g,' ').trim();
    if(t.indexOf(needle)>=0 && t.length<220 && e.children.length<=8) target=e;}
  if(!target) return {found:false};
  var el=target;
  for(var k=0;k<6 && el && el.parentElement;k++){
    var c=String(el.className||'');
    if(/row|item|session|card|listItem/i.test(c)||(el.getAttribute&&el.getAttribute('role')==='button')) break;
    el=el.parentElement;
  }
  (el||target).click();
  return {found:true, text:(target.innerText||'').replace(/\s+/g,' ').trim().slice(0,60)};
})()
