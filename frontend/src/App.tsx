import { useState, useEffect, useMemo } from 'react';
import { Upload, CheckCircle, RefreshCw, EyeOff, SplitSquareHorizontal, Eye, X, Plus } from 'lucide-react';
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip as RechartsTooltip, Legend } from 'recharts';
import { format, parseISO } from 'date-fns';
import './index.css';

// Types
interface Split {
  amount: number;
  category: string;
}

interface UPIData {
  merchant_or_person: string;
  bank: string;
  handle_or_gateway: string;
  note: string;
}

interface Transaction {
  id: string;
  merchant_name: string;
  amount: number;
  date: string;
  category: string;
  subcategory: string | null;
  verified: boolean;
  splits?: Split[] | null;
  upi?: UPIData | null;
}

const API_URL = 'http://localhost:8000/api';

function App() {
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [showPrivate, setShowPrivate] = useState(false);

  // Edit State
  const [editingTxnId, setEditingTxnId] = useState<string | null>(null);
  
  // Split Modal State
  const [splitTxn, setSplitTxn] = useState<Transaction | null>(null);
  const [splitValue1, setSplitValue1] = useState<number>(0);
  const [splitCat1, setSplitCat1] = useState<string>('Food & Dining');
  const [splitCat2, setSplitCat2] = useState<string>('Shared / Receivables');

  // Date Filtering
  const [filterMonth, setFilterMonth] = useState<string>('all');
  const [filterYear, setFilterYear] = useState<string>(new Date().getFullYear().toString());

  // Manual Transaction Modal
  const [showManualModal, setShowManualModal] = useState(false);
  const [manualAmount, setManualAmount] = useState('');
  const [manualMerchant, setManualMerchant] = useState('');
  const [manualNote, setManualNote] = useState('');
  const [manualDate, setManualDate] = useState(format(new Date(), "yyyy-MM-dd'T'HH:mm"));

  useEffect(() => {
    fetchData();
    fetchCategories();
  }, []);

  const fetchData = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/transactions`);
      const data = await res.json();
      setTransactions(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  const fetchCategories = async () => {
    try {
      const res = await fetch(`${API_URL}/categories`);
      const data = await res.json();
      setCategories(data.categories);
    } catch (e) {
      console.error(e);
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files || e.target.files.length === 0) return;
    const file = e.target.files[0];
    
    const formData = new FormData();
    formData.append("file", file);
    
    setUploading(true);
    try {
      const res = await fetch(`${API_URL}/upload`, {
        method: "POST",
        body: formData,
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail ?? "Upload failed.");
      alert(`Parsed ${body.parsed} transactions, ${body.added} new.`);
      await fetchData();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
      e.target.value = '';
    }
  };

  const handleCorrect = async (id: string, newCategory: string) => {
    try {
      const res = await fetch(`${API_URL}/transactions/${id}/correct`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ category: newCategory, subcategory: null })
      });
      
      if (!res.ok) {
        throw new Error("Server failed to register categorization.");
      }
      // The backend re-predicts every unverified row after a correction.
      await fetchData();
    } catch (e) {
      console.error(e);
      alert("Error: Core ML Engine failed to learn this transaction. Check backend logs.");
    }
  };

  const handleSplitSubmit = async () => {
    if (!splitTxn) return;
    const originalAmt = Math.abs(splitTxn.amount);
    if (splitValue1 <= 0 || splitValue1 >= originalAmt) {
      alert("Invalid split amount."); return;
    }
    
    const s2Amt = originalAmt - splitValue1;
    const splits = [
      { amount: splitTxn.amount >= 0 ? splitValue1 : -splitValue1, category: splitCat1 },
      { amount: splitTxn.amount >= 0 ? s2Amt : -s2Amt, category: splitCat2 }
    ];
    
    try {
      const res = await fetch(`${API_URL}/transactions/${splitTxn.id}/split`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ splits })
      });
      if (!res.ok) throw new Error((await res.json()).detail);
      setTransactions(prev => prev.map(t => t.id === splitTxn.id ? { ...t, splits } : t));
      setSplitTxn(null);
    } catch (e) {
      alert(e instanceof Error ? e.message : "Split failed.");
    }
  };

  const handleManualSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!manualAmount || !manualMerchant) return;
    
    setLoading(true);
    try {
      await fetch(`${API_URL}/transactions/manual`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          merchant_name: manualMerchant,
          amount: -Math.abs(Number(manualAmount)), // default to expense
          date: manualDate, // local time, e.g. 2026-03-01T14:30
          notes: manualNote
        })
      });
      setShowManualModal(false);
      setManualAmount(''); setManualMerchant(''); setManualNote('');
      await fetchData();
    } catch {
      alert("Failed to add transaction.");
    } finally {
      setLoading(false);
    }
  };

  const getCategoryClass = (category: string) => {
    if (!category) return 'bg-default';
    if (category === 'Uncategorized') return 'bg-uncategorized';
    if (category === 'Private') return 'bg-private';
    
    const lower = category.toLowerCase();
    if (lower.includes('food')) return 'bg-food';
    if (lower.includes('shop')) return 'bg-shopping';
    if (lower.includes('transport')) return 'bg-transport';
    
    return 'bg-default'; 
  };

  const filteredTransactions = transactions.filter(t => {
    if (t.category === 'Private' && !showPrivate) return false;
    
    const d = parseISO(t.date);
    if (filterYear !== 'all' && d.getFullYear().toString() !== filterYear) return false;
    if (filterMonth !== 'all' && d.getMonth().toString() !== filterMonth) return false;
    
    return true;
  });

  const categoryData = categories.map(c => {
    const value = filteredTransactions
      .filter(t => t.category === c && t.amount < 0 && c !== 'Uncategorized')
      .reduce((acc, t) => acc + Math.abs(t.amount), 0);
    return { name: c, value };
  }).filter(d => d.value > 0).sort((a,b) => b.value - a.value);

  // One entry per uncategorized merchant, most frequent first: labelling those
  // fixes the most rows (merchant memory applies the label to the rest).
  const uncategorized = useMemo(() => {
    const groups = new Map<string, Transaction[]>();
    for (const t of transactions) {
      if (t.category !== 'Uncategorized') continue;
      groups.set(t.merchant_name, [...(groups.get(t.merchant_name) ?? []), t]);
    }
    return [...groups.values()].sort((a, b) => b.length - a.length).map(g => g[0]);
  }, [transactions]);

  const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#06b6d4', '#f97316', '#ef4444'];


  return (
    <div className="app-container">
      <header>
        <div>
          <h1>Bachat AI</h1>
          <p style={{color: 'var(--text-muted)', marginTop: '4px'}}>Local Privacy-First Machine Learning</p>
        </div>
        <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
           {(loading || uploading) && <RefreshCw className="animate-spin text-muted" size={20} />}
           {transactions.length > 0 && (
             <label className="btn btn-secondary" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
               <Upload size={18} /> Upload statement
               <input type="file" className="hidden" accept=".csv,.xlsx" onChange={handleFileUpload} />
             </label>
           )}
           <button className="btn" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', background: 'rgba(59, 130, 246, 0.2)' }}
                   onClick={() => setShowManualModal(true)}>
             <Plus size={18} /> Add
           </button>
           <button className="btn btn-secondary" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}
                   onClick={() => setShowPrivate(!showPrivate)}>
             {showPrivate ? <Eye size={18} /> : <EyeOff size={18} />}
             {showPrivate ? "Vault Unlocked" : "Unlock Vault"}
           </button>
        </div>
      </header>

      <div className="dashboard-grid">
        {/* Main Content */}
        <div className="main-col">
          {transactions.length === 0 ? (
            <div className="glass-panel upload-zone">
              <input 
                type="file" 
                id="file-upload" 
                className="hidden" 
                accept=".csv,.xlsx" 
                onChange={handleFileUpload}
              />
              <label htmlFor="file-upload" style={{ cursor: 'pointer', display: 'block' }}>
                <Upload size={48} color="var(--accent-primary)" style={{ margin: '0 auto 1rem' }} />
                <h3>Upload Bank Statement</h3>
                <p style={{ color: 'var(--text-muted)', marginTop: '0.5rem' }}>Excel or CSV supported. 100% Local.</p>
                {uploading && <p style={{ color: '#10b981', marginTop: '1rem' }}>Processing ML categorizations...</p>}
              </label>
            </div>
          ) : (
            <div className="glass-panel" style={{ padding: '0', background: 'transparent', border: 'none', boxShadow: 'none' }}>
              
              {/* CHARTS & FILTERS DASHBOARD */}
              {transactions.length > 0 && (
                <div className="glass-panel" style={{ marginBottom: '1.5rem' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '1rem' }}>
                    <h3 style={{ margin: 0 }}>Spending Breakdown</h3>
                    <div style={{ display: 'flex', gap: '0.5rem' }}>
                      <select className="select-dropdown" value={filterMonth} onChange={e => setFilterMonth(e.target.value)} style={{ padding: '0.25rem 0.5rem' }}>
                        <option value="all">All Months</option>
                        {Array.from({length: 12}).map((_, i) => (
                          <option key={i} value={i.toString()}>{format(new Date(2024, i, 1), 'MMMM')}</option>
                        ))}
                      </select>
                      <select className="select-dropdown" value={filterYear} onChange={e => setFilterYear(e.target.value)} style={{ padding: '0.25rem 0.5rem' }}>
                        <option value="all">All Years</option>
                        {Array.from(new Set(transactions.map(t => parseISO(t.date).getFullYear()))).map(y => (
                          <option key={y} value={y.toString()}>{y}</option>
                        ))}
                      </select>
                    </div>
                  </div>
                  
                  {categoryData.length > 0 ? (
                    <div style={{ height: 250, width: '100%' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <PieChart>
                          <Pie
                            data={categoryData}
                            innerRadius={60}
                            outerRadius={80}
                            paddingAngle={5}
                            dataKey="value"
                          >
                            {categoryData.map((_, index) => (
                              <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                            ))}
                          </Pie>
                          <RechartsTooltip formatter={(val) => `₹${Number(val).toFixed(2)}`} contentStyle={{ background: '#1a1b26', border: '1px solid rgba(255,255,255,0.1)' }} />
                          <Legend verticalAlign="middle" align="right" layout="vertical" />
                        </PieChart>
                      </ResponsiveContainer>
                    </div>
                  ) : (
                    <p style={{ color: 'var(--text-muted)' }}>No expenses for this period.</p>
                  )}
                </div>
              )}

              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '1rem' }}>
                <h2>All Transactions ({filteredTransactions.length})</h2>
              </div>
              
              <div className="transaction-list">
                {filteredTransactions.map(t => (
                  <div key={t.id} className="transaction-item" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div className="t-left">
                        <div className="t-merchant">{t.merchant_name}</div>
                        {t.upi && (
                          <div style={{ marginTop: '0.25rem', fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                            <span style={{ display: 'inline-block', background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px', marginRight: '4px' }}>{t.upi.bank}</span>
                            <span style={{ display: 'inline-block', background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px', marginRight: '4px' }}>{t.upi.handle_or_gateway}</span>
                            {t.upi.note && <span style={{ fontStyle: 'italic' }}>"{t.upi.note}"</span>}
                          </div>
                        )}
                        <div className="t-date" style={{ marginTop: t.upi ? '4px' : '0' }}>{new Date(t.date).toLocaleDateString()}</div>
                        
                        {t.splits && t.splits.length > 0 && (
                          <div style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                             ↳ Split: ₹{Math.abs(t.splits[0].amount).toFixed(0)} ({t.splits[0].category}) / 
                                     ₹{Math.abs(t.splits[1].amount).toFixed(0)} ({t.splits[1].category})
                          </div>
                        )}
                      </div>
                      
                      <div className="t-right">
                        <div className={`t-amount ${t.amount >= 0 ? 't-amt-pos' : 't-amt-neg'}`}>
                          {t.amount >= 0 ? '+' : ''}₹{Math.abs(t.amount).toFixed(2)}
                        </div>
                        <div style={{ display: 'flex', gap: '0.5rem' }}>
                          <div 
                            className={`badge ${getCategoryClass(t.category)}`}
                            onClick={() => setEditingTxnId(editingTxnId === t.id ? null : t.id)}
                            style={{ cursor: 'pointer' }}
                            title="Click to change category"
                          >
                            {t.category === 'Private' && <EyeOff size={12} />}
                            {t.category}
                            {t.verified && <CheckCircle size={12} aria-label="confirmed by you" />}
                          </div>
                          <button onClick={() => { setSplitTxn(t); setSplitValue1(Math.abs(t.amount)/2); }} 
                                  style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-muted)' }}>
                             <SplitSquareHorizontal size={16} />
                          </button>
                        </div>
                      </div>
                    </div>
                    
                    {/* INLINE EDIT GRID */}
                    {editingTxnId === t.id && (
                      <div style={{ marginTop: '1rem', paddingTop: '1rem', borderTop: '1px solid rgba(255,255,255,0.05)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
                          <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>Re-categorizing "{t.merchant_name}":</span>
                          <button onClick={() => setEditingTxnId(null)} style={{ background: 'none', border: 'none', color: 'white', cursor: 'pointer' }}><X size={14} /></button>
                        </div>
                        <div className="category-grid">
                          {categories.map(c => (
                            <button 
                              key={c}
                              onClick={async () => {
                                await handleCorrect(t.id, c);
                                setEditingTxnId(null);
                              }}
                              className="cat-btn"
                              style={c === t.category ? { borderColor: 'var(--accent-primary)', background: 'rgba(59, 130, 246, 0.2)', color: 'white' } : {}}
                            >
                              {c}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Cold Start Side Panel */}
        <div className="side-col">
          <div className="glass-panel">
            <h3 style={{ marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
               Active Learning
               {uncategorized.length === 0 && transactions.length > 0 && <CheckCircle color="#10b981" size={20} />}
            </h3>
            
            {uncategorized.length > 0 ? (
              <div>
                <p style={{ color: 'var(--text-muted)', marginBottom: '1.5rem', fontSize: '0.9rem' }}>
                  {uncategorized.length} merchants need a label. Each answer also applies to that merchant's other rows and to similar names.
                </p>
                
                <div className="transaction-item" style={{ display: 'block', padding: '1.5rem', background: 'rgba(59, 130, 246, 0.05)', border: '1px solid rgba(59, 130, 246, 0.2)' }}>
                  <div style={{ textAlign: 'center', marginBottom: '1rem' }}>
                    <div className="t-merchant" style={{ fontSize: '1.4rem', marginBottom: '0.5rem' }}>{uncategorized[0].merchant_name}</div>
                    <div className="t-amount t-amt-neg">₹{Math.abs(uncategorized[0].amount).toFixed(2)}</div>
                  </div>
                  
                  <div className="category-grid">
                    {categories.filter(c => c !== 'Uncategorized').map(c => (
                      <button 
                        key={c}
                        onClick={() => handleCorrect(uncategorized[0].id, c)}
                        className="cat-btn"
                      >
                        {c}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            ) : (
              <p style={{ color: 'var(--text-muted)' }}>
                {transactions.length > 0 ? "All caught up." : "Upload a file to begin."}
              </p>
            )}
          </div>
        </div>
      </div>
      
      {/* SPLIT MODAL */}
      {splitTxn && (
        <div className="modal-overlay">
           <div className="glass-panel modal-content">
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '1rem' }}>
                 <h3>Split Transaction</h3>
                 <button onClick={() => setSplitTxn(null)} style={{ background: 'transparent', border: 'none', color: 'white', cursor: 'pointer' }}><X size={20} /></button>
              </div>
              <p style={{ color: 'var(--text-muted)', marginBottom: '1rem' }}>
                 {splitTxn.merchant_name} • Total: ₹{Math.abs(splitTxn.amount).toFixed(2)}
              </p>
              
              <div className="form-group">
                 <label>Your Share (₹)</label>
                 <input type="number" value={splitValue1} onChange={e => setSplitValue1(Number(e.target.value))} max={Math.abs(splitTxn.amount)} min={0} />
              </div>
              <div className="form-group">
                 <label>Category for Your Share</label>
                 <select value={splitCat1} onChange={e => setSplitCat1(e.target.value)}>
                    {categories.map(c => <option key={c} value={c}>{c}</option>)}
                 </select>
              </div>
              
              <div style={{ margin: '1.5rem 0', borderTop: '1px solid rgba(255,255,255,0.1)' }}></div>
              
              <div className="form-group">
                 <label>Friend's Share (IOU) (₹)</label>
                 <input type="number" value={Math.abs(splitTxn.amount) - splitValue1} readOnly disabled style={{ color: 'var(--text-muted)' }} />
              </div>
              <div className="form-group">
                 <label>Category for Friend's Share</label>
                 <select value={splitCat2} onChange={e => setSplitCat2(e.target.value)}>
                    {categories.map(c => <option key={c} value={c}>{c}</option>)}
                 </select>
              </div>
              
              <button className="btn" style={{ width: '100%', marginTop: '1rem' }} onClick={handleSplitSubmit}>Save Split</button>
           </div>
        </div>
      )}
      
      {/* MANUAL ENTRY MODAL */}
      {showManualModal && (
        <div className="modal-overlay">
           <div className="glass-panel modal-content">
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '1rem' }}>
                 <h3>Add Transaction</h3>
                 <button onClick={() => setShowManualModal(false)} style={{ background: 'transparent', border: 'none', color: 'white', cursor: 'pointer' }}><X size={20} /></button>
              </div>
              <form onSubmit={handleManualSubmit}>
                <div className="form-group">
                   <label>Merchant / Description</label>
                   <input type="text" required value={manualMerchant} onChange={e => setManualMerchant(e.target.value)} placeholder="e.g. Starbucks" />
                </div>
                <div className="form-group">
                   <label>Amount (₹)</label>
                   <input type="number" required step="0.01" value={manualAmount} onChange={e => setManualAmount(e.target.value)} placeholder="0.00" />
                </div>
                <div className="form-group">
                   <label>Date</label>
                   <input type="datetime-local" required value={manualDate} onChange={e => setManualDate(e.target.value)} />
                </div>
                <div className="form-group">
                   <label>Notes (Optional)</label>
                   <input type="text" value={manualNote} onChange={e => setManualNote(e.target.value)} placeholder="Additional details..." />
                </div>
                <button type="submit" className="btn" style={{ width: '100%', marginTop: '1rem' }}>Log Expense</button>
              </form>
           </div>
        </div>
      )}
    </div>
  );
}

export default App;
