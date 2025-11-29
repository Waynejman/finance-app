import os
import time
import urllib.parse
import hashlib
import re
from flask import Flask, render_template, request, redirect, url_for, flash, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime
from sqlalchemy import func
import csv
import io

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'

# --- 資料庫連線 (Render / Local 自動切換) ---
database_url = os.environ.get('DATABASE_URL')
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://")

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///finance.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = "請先登入以存取該頁面"
login_manager.login_message_category = "warning"

# --- 綠界測試環境設定 ---
ECPAY_MERCHANT_ID = '2000132'
ECPAY_HASH_KEY = '5294y06JbISpM5x9'
ECPAY_HASH_IV = 'v77hoKGq4kWxNNIS'
ECPAY_ACTION_URL = 'https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- 資料庫模型 ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    display_name = db.Column(db.String(50)) 
    bio = db.Column(db.String(200))
    fire_target = db.Column(db.Integer, default=10000000)
    is_premium = db.Column(db.Boolean, default=False) 
    
    transactions = db.relationship('Transaction', backref='owner', lazy=True)
    subscriptions = db.relationship('Subscription', backref='owner', lazy=True)
    achievements = db.relationship('UserAchievement', backref='owner', lazy=True)
    budgets = db.relationship('Budget', backref='owner', lazy=True)
    orders = db.relationship('Order', backref='owner', lazy=True)

    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)
    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trade_no = db.Column(db.String(50), unique=True, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="Pending") # Pending, Paid
    date_created = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, default=datetime.utcnow)
    amount = db.Column(db.Integer, nullable=False)
    type = db.Column(db.String(10), nullable=False)
    main_category = db.Column(db.String(50), nullable=False)
    item_name = db.Column(db.String(50), nullable=False)
    note = db.Column(db.String(200))
    mood = db.Column(db.String(20), default="neutral") # happy, neutral, regret
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class Budget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class Achievement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200))
    icon = db.Column(db.String(50)) 

class UserAchievement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    achievement_id = db.Column(db.Integer, db.ForeignKey('achievement.id'), nullable=False)
    date_earned = db.Column(db.Date, default=datetime.utcnow)

class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    date_sent = db.Column(db.DateTime, default=datetime.utcnow)

# --- 輔助函式 ---
def is_password_strong(password):
    if len(password) < 8: return False
    if not re.search(r"[a-z]", password): return False
    if not re.search(r"[A-Z]", password): return False
    if not re.search(r"[0-9]", password): return False
    return True

def get_mac_value(params):
    sorted_params = sorted(params.items())
    query_string = '&'.join([f'{k}={v}' for k, v in sorted_params])
    raw = f'HashKey={ECPAY_HASH_KEY}&{query_string}&HashIV={ECPAY_HASH_IV}'
    encoded = urllib.parse.quote_plus(raw).lower()
    encoded = encoded.replace('%21', '!').replace('%28', '(').replace('%29', ')')
    encoded = encoded.replace('%2a', '*').replace('%2d', '-').replace('%2e', '.')
    encoded = encoded.replace('%5f', '_')
    m = hashlib.md5()
    m.update(encoded.encode('utf-8'))
    return m.hexdigest().upper()

def init_achievements():
    try:
        default_achievements = [
            {"name": "記帳新手", "desc": "記下你的第一筆帳", "icon": "fa-baby"},
            {"name": "省錢達人", "desc": "單筆支出小於 50 元", "icon": "fa-piggy-bank"},
            {"name": "大戶人家", "desc": "單筆收入超過 5000 元", "icon": "fa-crown"},
            {"name": "訂閱管理者", "desc": "新增一筆訂閱服務", "icon": "fa-calendar-check"},
            {"name": "預算守門員", "desc": "設定你的第一個預算", "icon": "fa-shield-alt"}
        ]
        for ach in default_achievements:
            if not Achievement.query.filter_by(name=ach['name']).first():
                db.session.add(Achievement(name=ach['name'], description=ach['desc'], icon=ach['icon']))
        db.session.commit()
    except: pass

# 初始化資料庫
with app.app_context():
    # ★★★ 正式上線：請務必將下面這行註解掉 (加上 #)，防止資料被清空 ★★★
    # db.drop_all() 
    
    db.create_all()
    init_achievements()

def check_achievements(user, transaction=None, subscription=None, budget=None):
    earned = [ua.achievement_id for ua in user.achievements]
    if transaction and len(user.transactions) == 1: grant_achievement(user, "記帳新手", earned)
    if transaction and transaction.type == 'expense' and transaction.amount < 50: grant_achievement(user, "省錢達人", earned)
    if transaction and transaction.type == 'income' and transaction.amount > 5000: grant_achievement(user, "大戶人家", earned)
    if subscription: grant_achievement(user, "訂閱管理者", earned)
    if budget: grant_achievement(user, "預算守門員", earned)

def grant_achievement(user, ach_name, earned_ids):
    ach = Achievement.query.filter_by(name=ach_name).first()
    if ach and ach.id not in earned_ids:
        db.session.add(UserAchievement(user_id=user.id, achievement_id=ach.id))
        db.session.commit()
        flash(f"🏆 解鎖成就：{ach_name}！")

# --- 路由 ---

@app.route('/create_ecpay_order', methods=['POST'])
@login_required
def create_ecpay_order():
    order_id = f"FinanceApp{int(time.time())}" 
    order_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    amount = 100
    new_order = Order(trade_no=order_id, amount=amount, user_id=current_user.id, status="Pending")
    db.session.add(new_order); db.session.commit()
    params = {
        'MerchantID': ECPAY_MERCHANT_ID, 'MerchantTradeNo': order_id, 'MerchantTradeDate': order_time,
        'PaymentType': 'aio', 'TotalAmount': str(amount), 'TradeDesc': 'Upgrade to Premium',
        'ItemName': '記帳管家-付費會員', 'ReturnURL': 'https://www.example.com', 
        'ClientBackURL': url_for('ecpay_return', order_id=order_id, _external=True), 'ChoosePayment': 'ALL', 'EncryptType': '1',
    }
    params['CheckMacValue'] = get_mac_value(params)
    form_html = f'''<form id="ecpay_form" action="{ECPAY_ACTION_URL}" method="POST">{''.join([f'<input type="hidden" name="{k}" value="{v}">' for k, v in params.items()])}</form><script>document.getElementById("ecpay_form").submit();</script>'''
    return form_html

@app.route('/ecpay_return')
@login_required
def ecpay_return():
    order_id = request.args.get('order_id')
    order = Order.query.filter_by(trade_no=order_id).first()
    if order and order.user_id == current_user.id:
        order.status = "Paid"
        current_user.is_premium = True
        db.session.commit()
        flash('🎉 付款成功！感謝您的支持。')
    else: flash('⚠️ 訂單驗證失敗。')
    return redirect(url_for('settings'))

@app.route('/cancel_premium')
@login_required
def cancel_premium():
    if current_user.is_premium: current_user.is_premium = False; db.session.commit(); flash('⚠️ 已取消付費會員資格。')
    return redirect(url_for('settings'))

@app.route('/restore_purchase')
@login_required
def restore_purchase():
    paid_order = Order.query.filter_by(user_id=current_user.id, status="Paid").first()
    if paid_order: current_user.is_premium = True; db.session.commit(); flash('♻️ 恢復成功！')
    else: flash('❌ 查無付款紀錄。')
    return redirect(url_for('settings'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if not is_password_strong(password):
            flash('密碼強度不足！需包含至少8碼、大小寫英文及數字。')
            return redirect(url_for('register'))
        if User.query.filter_by(username=username).first():
            flash('帳號已存在！')
            return redirect(url_for('register'))
        new_user = User(username=username, display_name=username, bio="新手理財中", is_premium=False)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        flash('帳號或密碼錯誤')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect(url_for('login'))

@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':
        amount = int(request.form['amount'])
        t_type = request.form['type']
        main_cat = request.form['main_category']
        item = request.form['item_name']
        note = request.form['note']
        mood = request.form.get('mood', 'neutral')
        date_str = request.form.get('date')
        t_date = datetime.strptime(date_str, '%Y-%m-%d') if date_str else datetime.now()
        new_trans = Transaction(amount=amount, type=t_type, main_category=main_cat, item_name=item, note=note, mood=mood, date=t_date, owner=current_user)
        db.session.add(new_trans); db.session.commit()
        check_achievements(current_user, transaction=new_trans)
        return redirect(url_for('index'))

    current_month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    try: m_year, m_month = map(int, current_month.split('-'))
    except: today = datetime.now(); m_year, m_month = today.year, today.month; current_month = today.strftime('%Y-%m')

    transactions = Transaction.query.filter(Transaction.user_id == current_user.id, func.extract('year', Transaction.date) == m_year, func.extract('month', Transaction.date) == m_month).order_by(Transaction.date.desc()).all()
    all_income = db.session.query(func.sum(Transaction.amount)).filter_by(user_id=current_user.id, type='income').scalar() or 0
    all_expense = db.session.query(func.sum(Transaction.amount)).filter_by(user_id=current_user.id, type='expense').scalar() or 0
    net_worth = all_income - all_expense
    fire_progress = 0
    if current_user.fire_target > 0: fire_progress = min(100, int((net_worth / current_user.fire_target) * 100))

    return render_template('index.html', transactions=transactions, user=current_user, current_month=current_month, net_worth=net_worth, fire_progress=fire_progress)

@app.route('/analysis')
@login_required
def analysis():
    current_month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    try: m_year, m_month = map(int, current_month.split('-'))
    except: today = datetime.now(); m_year, m_month = today.year, today.month; current_month = today.strftime('%Y-%m')

    monthly_data = Transaction.query.filter(Transaction.user_id == current_user.id, func.extract('year', Transaction.date) == m_year, func.extract('month', Transaction.date) == m_month).order_by(Transaction.amount.desc()).all()
    expenses = [t for t in monthly_data if t.type == 'expense']
    incomes = [t for t in monthly_data if t.type == 'income']
    total_exp = sum(t.amount for t in expenses)
    total_inc = sum(t.amount for t in incomes)

    def group_data(data_list):
        grouped = {}
        for t in data_list:
            if t.main_category not in grouped: grouped[t.main_category] = {'total': 0, 'details': []}
            grouped[t.main_category]['total'] += t.amount
            grouped[t.main_category]['details'].append(t)
        return grouped

    exp_grouped = group_data(expenses)
    inc_grouped = group_data(incomes)

    user_budgets = Budget.query.filter_by(user_id=current_user.id).all()
    budget_analysis = []
    
    # 情緒消費
    regret_amount = sum(t.amount for t in expenses if t.mood == 'regret')
    regret_percent = 0
    if total_exp > 0: regret_percent = int((regret_amount / total_exp) * 100)

    if current_user.is_premium:
        for b in user_budgets:
            spent = exp_grouped.get(b.category, {'total': 0})['total']
            if b.amount > 0: percent = min(100, int((spent / b.amount) * 100))
            else: percent = 100 if spent > 0 else 0
            status = "danger" if percent >= 100 else ("warning" if percent >= 80 else "success")
            budget_analysis.append({"category": b.category, "limit": b.amount, "spent": spent, "percent": percent, "status": status})

    ai_advice = ""
    if current_user.is_premium:
        top_cat = max(exp_grouped, key=lambda k: exp_grouped[k]['total']) if exp_grouped else None
        if regret_percent > 20: ai_advice = f"⚠️ 警報！本月有 {regret_percent}% 的支出是「後悔消費」，建議在下次付款前多想 3 秒鐘。"
        elif total_inc > 0:
            rate = (total_inc - total_exp) / total_inc
            if rate < 0: ai_advice = f"本月已透支！最大支出為「{top_cat}」。"
            elif rate < 0.2: ai_advice = "儲蓄率偏低，建議設定預算來控制花費。"
            else: ai_advice = "儲蓄率健康！繼續保持快樂理財。"
        elif total_exp > 0: ai_advice = "本月尚無收入，但已有支出。"
        else: ai_advice = "目前沒有資料。"
    else: ai_advice = "🔒 [付費限定] 升級會員以解鎖 AI 財務診斷、情緒消費分析與預算監控。"

    return render_template('analysis.html', 
                           exp_grouped=exp_grouped, inc_grouped=inc_grouped,
                           exp_labels=list(exp_grouped.keys()), exp_values=[d['total'] for d in exp_grouped.values()],
                           inc_labels=list(inc_grouped.keys()), inc_values=[d['total'] for d in inc_grouped.values()],
                           total_expense=total_exp, total_income=total_inc,
                           current_month=current_month, user=current_user, ai_advice=ai_advice,
                           budget_analysis=budget_analysis, regret_amount=regret_amount, regret_percent=regret_percent)

@app.route('/add_subscription', methods=['POST'])
@login_required
def add_subscription():
    if not current_user.is_premium: flash('🔒 請先升級。'); return redirect(url_for('settings'))
    name = request.form['name']; amount = int(request.form['amount'])
    sub = Subscription(name=name, amount=amount, owner=current_user)
    db.session.add(sub); db.session.commit(); check_achievements(current_user, subscription=sub)
    return redirect(url_for('settings'))

@app.route('/delete_subscription/<int:id>')
@login_required
def delete_subscription(id):
    sub = Subscription.query.get_or_404(id)
    if sub.user_id == current_user.id: db.session.delete(sub); db.session.commit()
    return redirect(url_for('settings'))

@app.route('/update_budget', methods=['POST'])
@login_required
def update_budget():
    if not current_user.is_premium: flash('🔒 請先升級。'); return redirect(url_for('settings'))
    categories = ["餐飲", "交通", "娛樂", "購物", "房租", "其他"]
    for cat in categories:
        amount_str = request.form.get(f'budget_{cat}')
        if amount_str and amount_str.strip():
            try:
                amount = int(amount_str)
                existing = Budget.query.filter_by(user_id=current_user.id, category=cat).first()
                if existing: existing.amount = amount
                else: db.session.add(Budget(category=cat, amount=amount, owner=current_user))
            except ValueError: pass
    db.session.commit(); check_achievements(current_user, budget=True); flash('預算設定已更新！')
    return redirect(url_for('settings'))

@app.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    current_user.display_name = request.form['display_name']; current_user.bio = request.form['bio']
    try: current_user.fire_target = int(request.form['fire_target'])
    except ValueError: pass
    db.session.commit(); flash('設定已更新！')
    return redirect(url_for('settings'))

@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    old_pw = request.form['old_password']; new_pw = request.form['new_password']
    if not is_password_strong(new_pw): flash('新密碼強度不足 (需8碼+大小寫英文+數字)'); return redirect(url_for('settings'))
    if not current_user.check_password(old_pw): flash('舊密碼錯誤！')
    else: current_user.set_password(new_pw); db.session.commit(); flash('密碼修改成功！')
    return redirect(url_for('settings'))

@app.route('/submit_feedback', methods=['POST'])
@login_required
def submit_feedback():
    message = request.form['message']
    if message: fb = Feedback(user_id=current_user.id, message=message); db.session.add(fb); db.session.commit(); flash('感謝您的回饋！')
    return redirect(url_for('settings'))

@app.route('/export_csv')
@login_required
def export_csv():
    if not current_user.is_premium: flash('🔒 請先升級。'); return redirect(url_for('settings'))
    all_trans = Transaction.query.filter_by(user_id=current_user.id).order_by(Transaction.date.desc()).all()
    output = io.StringIO(); output.write(u'\ufeff'); writer = csv.writer(output)
    writer.writerow(['日期', '收支類型', '主分類', '細項', '金額', '消費情緒', '備註'])
    for t in all_trans:
        t_type_zh = "支出" if t.type == "expense" else "收入"
        mood_zh = "😐 需要"
        if t.mood == 'happy': mood_zh = "😊 值得"
        elif t.mood == 'regret': mood_zh = "😫 後悔"
        writer.writerow([t.date.strftime('%Y-%m-%d'), t_type_zh, t.main_category, t.item_name, t.amount, mood_zh, t.note])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-disposition": "attachment; filename=finance_report.csv"})

@app.route('/settings')
@login_required
def settings():
    user_achievements = [ua.achievement_id for ua in current_user.achievements]
    all_achievements = Achievement.query.all()
    ach_list = [{"name": a.name, "desc": a.description, "icon": a.icon, "unlocked": a.id in user_achievements} for a in all_achievements]
    current_budgets = {b.category: b.amount for b in current_user.budgets}
    return render_template('settings.html', user=current_user, achievements=ach_list, current_budgets=current_budgets)

@app.route('/delete/<int:id>')
@login_required
def delete(id):
    t = Transaction.query.get_or_404(id)
    if t.user_id == current_user.id: db.session.delete(t); db.session.commit()
    return redirect(request.referrer or url_for('index'))

@app.errorhandler(404)
def page_not_found(e): return render_template('404.html'), 404
@app.errorhandler(500)
def internal_server_error(e): return render_template('500.html'), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)