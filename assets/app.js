// Shared helpers
function $(sel, root=document){ return root.querySelector(sel); }
function $all(sel, root=document){ return Array.from(root.querySelectorAll(sel)); }

function setActiveNav() {
  const path = location.pathname.split('/').pop() || 'index.html';
  $all('.nav a').forEach(a => {
    const href = a.getAttribute('href');
    if (href === path) a.classList.add('active');
    else a.classList.remove('active');
  });
}

function moneyINR(v){
  if (typeof v !== 'number') return '-';
  try {
    return new Intl.NumberFormat('en-IN', { style:'currency', currency:'INR', maximumFractionDigits:0 }).format(v);
  } catch { return `₹${Math.round(v).toLocaleString('en-IN')}`; }
}

function stockPill(qty){
  if (qty === null || qty === undefined) return {label:'Stock: -', cls:'warn'};
  if (qty <= 0) return {label:`Out of stock`, cls:'bad'};
  if (qty <= 2) return {label:`Low stock: ${qty}`, cls:'warn'};
  return {label:`In stock: ${qty}`, cls:'ok'};
}

async function loadData(path){
  const res = await fetch(path);
  if (!res.ok) throw new Error(`Failed to load ${path}`);
  return res.json();
}

function getParam(name){
  return new URLSearchParams(location.search).get(name);
}

function safeText(s){
  return (s ?? '').toString().replace(/[<>]/g, '');
}

setActiveNav();
