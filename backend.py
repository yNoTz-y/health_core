from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_caching import Cache
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
import re
import logging
import os
from datetime import datetime, date, timedelta
import jwt
from functools import wraps
from dotenv import load_dotenv
import math
from gerador_relatorio import (
    summarize_csv,
    generate_pdf_report,
    load_csv_patients,
    load_csv_patient_details,
    load_csv_preview,
    parse_csv_date,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configurar Flask para servir arquivos estáticos do dashboard
app = Flask(__name__, 
    static_folder=os.path.join(os.path.dirname(__file__), 'dashboard'),
    static_url_path='/dashboard'
)

# Em produção, troque pelas origens reais do frontend
CORS(app)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# CONFIGURAÇÃO POR AMBIENTE
ENV = os.environ.get('FLASK_ENV', 'development')
if ENV == 'production':
    DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://user:password@localhost/healthcore_db')
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': 20,
        'pool_recycle': 3600,
        'pool_pre_ping': True,
        'echo': False
    }
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///healthreport.db')
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'echo': os.environ.get('DEBUG_SQL', 'false').lower() == 'true'
    }

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# CACHE CONFIGURATION
app.config['CACHE_TYPE'] = os.environ.get('CACHE_TYPE', 'simple')  # 'simple', 'redis'
app.config['CACHE_DEFAULT_TIMEOUT'] = 3600  # 1 hora ao invés de 300 segundos
if os.environ.get('CACHE_TYPE') == 'redis':
    app.config['CACHE_REDIS_URL'] = os.environ.get('CACHE_REDIS_URL', 'redis://localhost:6379/0')

db = SQLAlchemy(app)
cache = Cache(app)

# MODELOS

class User(db.Model):
    __tablename__ = 'users'
    
    __table_args__ = (
        db.Index('idx_user_email', 'email'),
        db.Index('idx_user_role', 'role'),
        db.Index('idx_user_created', 'created_at'),
    )

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(50), nullable=False, default='health_staff')
    specialty = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'full_name': self.full_name,
            'role': self.role,
            'specialty': self.specialty,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Patient(db.Model):
    __tablename__ = 'patients'
    
    __table_args__ = (
        db.Index('idx_patient_name', 'full_name'),
        db.Index('idx_patient_email', 'email'),
        db.Index('idx_patient_created', 'created_at'),
        db.Index('idx_patient_city', 'city'),
        db.Index('idx_patient_city_name', 'city', 'full_name'),
    )

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    birth_date = db.Column(db.Date, nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    document_number = db.Column(db.String(30), unique=True, nullable=True)
    city = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def age(self):
        today = date.today()
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years

    def to_dict(self):
        return {
            'id': self.id,
            'full_name': self.full_name,
            'birth_date': self.birth_date.isoformat() if self.birth_date else None,
            'age': self.age() if self.birth_date else None,
            'gender': self.gender,
            'phone': self.phone,
            'email': self.email,
            'document_number': self.document_number,
            'city': self.city,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Appointment(db.Model):
    __tablename__ = 'appointments'
    
    __table_args__ = (
        db.Index('idx_appt_patient_id', 'patient_id'),
        db.Index('idx_appt_prof_id', 'professional_id'),
        db.Index('idx_appt_status', 'status'),
        db.Index('idx_appt_date', 'appointment_date'),
        db.Index('idx_appt_status_date', 'status', 'appointment_date'),
        db.Index('idx_appt_patient_date', 'patient_id', 'appointment_date'),
    )

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    professional_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    appointment_date = db.Column(db.DateTime, nullable=False)
    appointment_type = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(30), nullable=False, default='scheduled')
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship('Patient', backref='appointments')
    professional = db.relationship('User', backref='appointments')

    def to_dict(self):
        return {
            'id': self.id,
            'patient_id': self.patient_id,
            'patient_name': self.patient.full_name if self.patient else None,
            'professional_id': self.professional_id,
            'professional_name': self.professional.full_name if self.professional else None,
            'appointment_date': self.appointment_date.isoformat() if self.appointment_date else None,
            'appointment_type': self.appointment_type,
            'status': self.status,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Exam(db.Model):
    __tablename__ = 'exams'
    
    __table_args__ = (
        db.Index('idx_exam_patient_id', 'patient_id'),
        db.Index('idx_exam_requested_by', 'requested_by'),
        db.Index('idx_exam_status', 'status'),
        db.Index('idx_exam_type', 'exam_type'),
        db.Index('idx_exam_requested_at', 'requested_at'),
        db.Index('idx_exam_status_requested', 'status', 'requested_at'),
        db.Index('idx_exam_patient_status', 'patient_id', 'status'),
    )

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    exam_type = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(30), nullable=False, default='pending')
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    result_summary = db.Column(db.Text, nullable=True)

    patient = db.relationship('Patient', backref='exams')
    professional = db.relationship('User', backref='requested_exams')

    def to_dict(self):
        return {
            'id': self.id,
            'patient_id': self.patient_id,
            'patient_name': self.patient.full_name if self.patient else None,
            'requested_by': self.requested_by,
            'requested_by_name': self.professional.full_name if self.professional else None,
            'exam_type': self.exam_type,
            'status': self.status,
            'requested_at': self.requested_at.isoformat() if self.requested_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'result_summary': self.result_summary
        }

# FUNÇÕES AUXILIARES

def validate_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def parse_date(date_str, field_name='data'):
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except Exception:
        raise ValueError(f'{field_name} inválida. Use o formato YYYY-MM-DD.')


def parse_datetime(datetime_str, field_name='data/hora'):
    try:
        return datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S')
    except Exception:
        raise ValueError(f'{field_name} inválida. Use o formato YYYY-MM-DD HH:MM:SS.')


def normalize_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def normalize_document_number(value):
    if value is None:
        return None
    normalized = re.sub(r'\D', '', str(value))
    return normalized if normalized else None


def commit_patient_batch(batch):
    """Tenta gravar um lote de pacientes e realiza fallback para commits individuais em caso de conflito."""
    if not batch:
        return 0, 0

    patients = [item[0] for item in batch]
    try:
        db.session.add_all(patients)
        db.session.commit()
        return len(patients), 0
    except IntegrityError:
        db.session.rollback()
        inserted = 0
        errors = 0
        for patient, row_index in batch:
            try:
                db.session.add(patient)
                db.session.commit()
                inserted += 1
            except IntegrityError:
                db.session.rollback()
                logger.warning(f'Linha {row_index}: conflito de dados durante importação, registro não inserido.')
                errors += 1
            except Exception as exc:
                db.session.rollback()
                logger.error(f'Linha {row_index}: erro ao gravar paciente: {str(exc)}')
                errors += 1
        return inserted, errors


def paginate_query(query, page=None, per_page=None):
    """Paginação de resultados com limites de segurança"""
    if page is None:
        page = max(1, int(request.args.get('page', 1)))
    if per_page is None:
        per_page = int(request.args.get('per_page', 50))
    
    per_page = min(per_page, 100)
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    return {
        'items': pagination.items,
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page,
        'per_page': per_page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }


def seed_users():
    if not User.query.filter_by(email='admin@clinic.com').first():
        admin = User(
            email='admin@clinic.com',
            password_hash=generate_password_hash('admin123'),
            full_name='Administrador Clínico',
            role='admin',
            specialty='Gestão',
            is_active=True
        )
        db.session.add(admin)
        db.session.commit()
        logger.info('Usuário administrador criado com sucesso')


def seed_patients():
    if Patient.query.count() > 0:
        return

    patients = [
        Patient(
            full_name='Ana Souza',
            birth_date=date(1990, 5, 14),
            gender='Feminino',
            phone='(14) 99999-1111',
            email='ana.souza@email.com',
            document_number='11111111111',
            city='Marília'
        ),
        Patient(
            full_name='Carlos Mendes',
            birth_date=date(1982, 8, 22),
            gender='Masculino',
            phone='(14) 99999-2222',
            email='carlos.mendes@email.com',
            document_number='22222222222',
            city='Bauru'
        ),
        Patient(
            full_name='Juliana Lima',
            birth_date=date(1975, 1, 30),
            gender='Feminino',
            phone='(14) 99999-3333',
            email='juliana.lima@email.com',
            document_number='33333333333',
            city='Assis'
        ),
        Patient(
            full_name='Roberto Alves',
            birth_date=date(2001, 11, 12),
            gender='Masculino',
            phone='(14) 99999-4444',
            email='roberto.alves@email.com',
            document_number='44444444444',
            city='Marília'
        ),
        Patient(
            full_name='Mariana Costa',
            birth_date=date(1968, 3, 9),
            gender='Feminino',
            phone='(14) 99999-5555',
            email='mariana.costa@email.com',
            document_number='55555555555',
            city='Lins'
        )
    ]

    db.session.add_all(patients)
    db.session.commit()
    logger.info('Pacientes iniciais cadastrados com sucesso')


def seed_appointments_and_exams():
    if Appointment.query.count() > 0 or Exam.query.count() > 0:
        return

    admin = User.query.filter_by(email='admin@clinic.com').first()
    patients = Patient.query.order_by(Patient.id.asc()).all()

    if not admin or not patients:
        return

    base_now = datetime.utcnow()

    appointments = [
        Appointment(
            patient_id=patients[0].id,
            professional_id=admin.id,
            appointment_date=base_now - timedelta(days=90),
            appointment_type='Consulta de rotina',
            status='completed',
            notes='Paciente estável'
        ),
        Appointment(
            patient_id=patients[1].id,
            professional_id=admin.id,
            appointment_date=base_now - timedelta(days=60),
            appointment_type='Retorno',
            status='completed',
            notes='Ajuste medicamentoso'
        ),
        Appointment(
            patient_id=patients[2].id,
            professional_id=admin.id,
            appointment_date=base_now - timedelta(days=30),
            appointment_type='Primeira consulta',
            status='completed',
            notes='Encaminhada para exame'
        ),
        Appointment(
            patient_id=patients[3].id,
            professional_id=admin.id,
            appointment_date=base_now - timedelta(days=10),
            appointment_type='Avaliação clínica',
            status='scheduled',
            notes='Agendada'
        ),
        Appointment(
            patient_id=patients[4].id,
            professional_id=admin.id,
            appointment_date=base_now - timedelta(days=5),
            appointment_type='Consulta de rotina',
            status='completed',
            notes='Solicitado exame complementar'
        ),
        Appointment(
            patient_id=patients[0].id,
            professional_id=admin.id,
            appointment_date=base_now + timedelta(days=3),
            appointment_type='Retorno',
            status='scheduled',
            notes='Retorno agendado'
        )
    ]

    exams = [
        Exam(
            patient_id=patients[0].id,
            requested_by=admin.id,
            exam_type='Hemograma',
            status='completed',
            requested_at=base_now - timedelta(days=20),
            completed_at=base_now - timedelta(days=15),
            result_summary='Resultados dentro da normalidade'
        ),
        Exam(
            patient_id=patients[2].id,
            requested_by=admin.id,
            exam_type='Raio-X',
            status='pending',
            requested_at=base_now - timedelta(days=12),
            completed_at=None,
            result_summary=None
        ),
        Exam(
            patient_id=patients[4].id,
            requested_by=admin.id,
            exam_type='Ressonância magnética',
            status='pending',
            requested_at=base_now - timedelta(days=4),
            completed_at=None,
            result_summary=None
        ),
        Exam(
            patient_id=patients[1].id,
            requested_by=admin.id,
            exam_type='Glicemia',
            status='completed',
            requested_at=base_now - timedelta(days=50),
            completed_at=base_now - timedelta(days=45),
            result_summary='Leve alteração glicêmica'
        )
    ]

    db.session.add_all(appointments)
    db.session.add_all(exams)
    db.session.commit()
    logger.info('Consultas e exames iniciais cadastrados com sucesso')


def create_tables_and_seed():
    try:
        db.create_all()
        seed_users()
        seed_patients()
        seed_appointments_and_exams()
    except Exception as e:
        db.session.rollback()
        logger.error(f'Erro ao criar tabelas ou seed: {str(e)}')


def import_csv_to_db(limit=None, batch_size=500):
    """Importa pacientes do CSV para a tabela `patients`.

    Retorna um dicionário com estatísticas: inserted, skipped, errors.
    """
    inserted = 0
    skipped = 0
    errors = 0

    rows = load_csv_patients(limit=limit)

    to_add = []
    seen_documents = set()
    seen_name_birth = set()

    for row_index, r in enumerate(rows, start=1):
        try:
            doc = normalize_document_number(r.get('document_number') or r.get('cpf') or None)
            name = normalize_text(r.get('full_name')) or 'Paciente sem nome'
            birth_str = normalize_text(r.get('birth_date'))

            # birth_date is required by model
            birth_dt = parse_csv_date(birth_str) if birth_str else None
            if birth_dt is None:
                skipped += 1
                logger.warning(f'Linha {row_index}: data de nascimento inválida ou ausente -> {birth_str}')
                continue

            birth_date = birth_dt.date()
            gender = normalize_text(r.get('gender')) or 'Não informado'
            phone = normalize_text(r.get('phone')) or None
            city = normalize_text(r.get('city')) or None

            name_key = (name.lower(), birth_date)
            if doc and doc in seen_documents:
                skipped += 1
                logger.warning(f'Linha {row_index}: documento duplicado no lote -> {doc}')
                continue
            if name_key in seen_name_birth:
                skipped += 1
                logger.warning(f'Linha {row_index}: paciente duplicado no lote -> {name} / {birth_date}')
                continue

            existing = None
            if doc:
                existing = Patient.query.filter_by(document_number=doc).first()

            if not existing:
                existing = Patient.query.filter(func.lower(Patient.full_name) == name.lower(), Patient.birth_date == birth_date).first()

            if existing:
                skipped += 1
                continue

            if doc:
                seen_documents.add(doc)
            seen_name_birth.add(name_key)

            patient = Patient(
                full_name=name,
                birth_date=birth_date,
                gender=gender,
                phone=phone,
                email=None,
                document_number=doc,
                city=city
            )

            to_add.append((patient, row_index))

            if len(to_add) >= batch_size:
                batch_inserted, batch_errors = commit_patient_batch(to_add)
                inserted += batch_inserted
                errors += batch_errors
                to_add = []

        except Exception as e:
            logger.error(f'Linha {row_index}: Erro ao importar linha CSV: {str(e)}')
            db.session.rollback()
            errors += 1

    if to_add:
        batch_inserted, batch_errors = commit_patient_batch(to_add)
        inserted += batch_inserted
        errors += batch_errors

    return {'inserted': inserted, 'skipped': skipped, 'errors': errors, 'total_read': len(rows)}


@app.route('/import-csv', methods=['POST', 'GET'])
def import_csv_endpoint():
    try:
        try:
            limit = request.args.get('limit')
            limit = int(limit) if limit is not None else None
        except Exception:
            limit = None

        stats = import_csv_to_db(limit=limit)
        return jsonify({"success": True, "stats": stats}), 200
    except Exception as e:
        logger.error(f'Erro ao importar CSV para DB: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao importar CSV"}), 500

# DECORADOR DE AUTENTICAÇÃO

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # Verificar token no header Authorization
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            try:
                token = auth_header.split(" ")[1]
            except IndexError:
                return jsonify({"success": False, "message": "Token inválido"}), 401
        
        if not token:
            return jsonify({"success": False, "message": "Token ausente. Acesso não autorizado"}), 401
        
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user_id = data.get('user_id')
            current_user = User.query.get(current_user_id)
            
            if not current_user or not current_user.is_active:
                return jsonify({"success": False, "message": "Usuário não encontrado ou inativo"}), 401
                
        except jwt.ExpiredSignatureError:
            return jsonify({"success": False, "message": "Token expirado"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"success": False, "message": "Token inválido"}), 401
        
        return f(current_user, *args, **kwargs)
    
    return decorated


@app.route('/verify-auth', methods=['GET'])
def verify_auth():
    """Endpoint para verificar autenticação - agora sem restrição"""
    try:
        # Retornar status de disponibilidade do servidor
        admin = User.query.filter_by(email='admin@clinic.com').first()
        return jsonify({
            "success": True,
            "message": "Sistema disponível",
            "user": admin.to_dict() if admin else {"email": "sistema@clinic.com", "full_name": "Sistema"}
        }), 200
    except Exception as e:
        logger.error(f'Erro ao verificar disponibilidade: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao verificar disponibilidade"}), 500


@app.route('/register', methods=['POST'])
def register():
    try:
        if not request.is_json:
            return jsonify({"success": False, "message": "Content-Type deve ser application/json"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "Dados não fornecidos"}), 400

        email = data.get("email", "").strip()
        password = data.get("password", "").strip()
        confirm_password = data.get("confirm_password", "").strip()
        full_name = data.get("full_name", "").strip()
        birth_date_str = data.get("birth_date", "").strip()

        # Validações básicas
        if not email:
            return jsonify({"success": False, "message": "Email é obrigatório"}), 400

        if not validate_email(email):
            return jsonify({"success": False, "message": "Formato de email inválido"}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({"success": False, "message": "Email já cadastrado"}), 409

        if not password:
            return jsonify({"success": False, "message": "Senha é obrigatória"}), 400

        if len(password) < 6:
            return jsonify({"success": False, "message": "Senha deve ter no mínimo 6 caracteres"}), 400

        if password != confirm_password:
            return jsonify({"success": False, "message": "Senhas não conferem"}), 400

        if not full_name:
            return jsonify({"success": False, "message": "Nome completo é obrigatório"}), 400

        if not birth_date_str:
            return jsonify({"success": False, "message": "Data de nascimento é obrigatória"}), 400

        # Parse da data de nascimento
        try:
            birth_date = datetime.strptime(birth_date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"success": False, "message": "Formato de data inválido. Use YYYY-MM-DD"}), 400

        # Criar novo usuário
        new_user = User(
            email=email,
            password_hash=generate_password_hash(password),
            full_name=full_name,
            role='health_staff',
            specialty=None,
            is_active=True
        )

        db.session.add(new_user)
        db.session.commit()

        logger.info(f'Novo usuário registrado: {email}')

        return jsonify({
            "success": True,
            "message": "Cadastro realizado com sucesso!",
            "user": new_user.to_dict()
        }), 201

    except Exception as e:
        db.session.rollback()
        logger.error(f'Erro ao registrar usuário: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao criar conta"}), 500

# DASHBOARD / RELATÓRIOS

@app.route('/dashboard-summary', methods=['GET'])
@cache.cached(timeout=300)
def dashboard_summary():
    try:
        csv_summary = summarize_csv(limit=None)
        total_patients = csv_summary['total_records']
        total_appointments = csv_summary.get('total_appointments', 0)
        pending_exams = csv_summary.get('pending_exams', 0)
        return_rate = csv_summary.get('return_rate', 0)

        return jsonify({
            "success": True,
            "total_patients": total_patients,
            "total_appointments": total_appointments,
            "pending_exams": pending_exams,
            "return_rate": return_rate
        }), 200

    except Exception as e:
        logger.error(f'Erro ao carregar resumo do dashboard: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao carregar resumo"}), 500


@app.route('/appointments-by-month', methods=['GET'])
@cache.cached(timeout=300)
def appointments_by_month():
    try:
        csv_summary = summarize_csv(limit=None)
        data = [{"month": label, "total": total} for label, total in csv_summary['birth_month_counts'].items()]

        if not data:
            data = [{"month": label, "total": total} for label, total in csv_summary['age_groups'].items()]

        return jsonify({
            "success": True,
            "data": data
        }), 200

    except Exception as e:
        logger.error(f'Erro ao buscar consultas por mês: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao carregar gráfico de consultas"}), 500


@app.route('/patients-by-gender', methods=['GET'])
@cache.cached(timeout=300)
def patients_by_gender():
    try:
        csv_summary = summarize_csv(limit=None)
        data = [{"gender": label, "total": total} for label, total in csv_summary['gender_counts'].items()]

        return jsonify({
            "success": True,
            "data": data
        }), 200

    except Exception as e:
        logger.error(f'Erro ao buscar pacientes por sexo: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao carregar gráfico de pacientes"}), 500


@app.route('/patients-by-city', methods=['GET'])
@cache.cached(timeout=300)
def patients_by_city():
    try:
        csv_summary = summarize_csv(limit=None)
        data = [{"city": label, "total": total} for label, total in csv_summary['city_counts'].items()]

        return jsonify({
            "success": True,
            "data": data
        }), 200
    except Exception as e:
        logger.error(f'Erro ao buscar pacientes por cidade: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao carregar gráfico de pacientes por cidade"}), 500


@app.route('/patients-by-age-group', methods=['GET'])
@cache.cached(timeout=300)
def patients_by_age_group():
    try:
        csv_summary = summarize_csv(limit=None)
        data = [{"age_group": label, "total": total} for label, total in csv_summary['age_groups'].items()]

        return jsonify({
            "success": True,
            "data": data
        }), 200
    except Exception as e:
        logger.error(f'Erro ao buscar faixa etária: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao carregar gráfico de faixa etária"}), 500


@app.route('/patients-by-state', methods=['GET'])
@app.route('/exams-by-status', methods=['GET'])
@cache.cached(timeout=300)
def patients_by_state():
    try:
        csv_summary = summarize_csv(limit=None)
        data = [{"state": label, "total": total} for label, total in csv_summary['state_counts'].items()]

        return jsonify({
            "success": True,
            "data": data
        }), 200

    except Exception as e:
        logger.error(f'Erro ao buscar pacientes por estado: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao carregar gráfico de pacientes por estado"}), 500


@app.route('/csv-dashboard-summary', methods=['GET'])
def csv_dashboard_summary():
    try:
        summary = summarize_csv(limit=None)
        return jsonify({
            "success": True,
            "data": summary
        }), 200
    except Exception as e:
        logger.error(f'Erro ao gerar resumo CSV: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao gerar resumo CSV"}), 500


@app.route('/csv-preview', methods=['GET'])
def csv_preview():
    try:
        try:
            limit_param = request.args.get('limit')
            if limit_param is None:
                limit = 100
            else:
                lp = str(limit_param).lower()
                limit = None if lp in ('none', 'null', '') else int(limit_param)
        except Exception:
            limit = 100

        rows = load_csv_preview(limit=limit)
        return jsonify({"success": True, "rows": rows}), 200
    except FileNotFoundError:
        return jsonify({"success": False, "message": "Arquivo CSV não encontrado."}), 404
    except Exception as e:
        logger.error(f'Erro ao gerar preview CSV: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao gerar preview CSV"}), 500


@app.route('/generate-report-pdf', methods=['GET'])
def generate_report_pdf():
    try:
        output_path = generate_pdf_report()
        return send_file(
            output_path,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=os.path.basename(output_path)
        )
    except Exception as e:
        logger.error(f'Erro ao gerar PDF: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao gerar PDF"}), 500


@app.route('/patients', methods=['GET'])
def get_patients():
    try:
        # Preferir ler da base de dados se já houver pacientes importados (mais rápido)
        total_db = Patient.query.count()
        if total_db > 0:
            # paginação simples: ?page=1&per_page=50
            page = max(1, int(request.args.get('page', 1)))
            per_page = int(request.args.get('per_page', 50))
            per_page = min(per_page, 500)

            query = Patient.query.order_by(Patient.full_name.asc())
            pagination = query.paginate(page=page, per_page=per_page, error_out=False)

            return jsonify({
                "success": True,
                "patients": [p.to_dict() for p in pagination.items],
                "pagination": {
                    "total": pagination.total,
                    "pages": pagination.pages,
                    "current_page": page,
                    "per_page": per_page,
                    "has_next": pagination.has_next,
                    "has_prev": pagination.has_prev
                }
            }), 200

        # Fallback: carregar do CSV (se não houver dados no DB)
        try:
            limit = request.args.get('limit')
            limit = int(limit) if limit is not None else 2000
        except Exception:
            limit = 2000

        patients = load_csv_patients(limit=limit)

        return jsonify({
            "success": True,
            "patients": patients,
            "pagination": {
                "total": len(patients),
                "pages": 1,
                "current_page": 1,
                "per_page": len(patients),
                "has_next": False,
                "has_prev": False
            }
        }), 200
    except Exception as e:
        logger.error(f'Erro ao listar pacientes: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao buscar pacientes"}), 500


@app.route('/patient-report/<int:patient_id>', methods=['GET'])
def patient_report(patient_id):
    try:
        patient_details = load_csv_patient_details(patient_id, limit=None)

        if not patient_details:
            return jsonify({"success": False, "message": "Paciente não encontrado"}), 404

        return jsonify({
            "success": True,
            "patient": patient_details['patient'],
            "appointments": patient_details['appointments'],
            "exams": patient_details['exams']
        }), 200

    except Exception as e:
        logger.error(f'Erro ao gerar relatório do paciente: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao gerar relatório do paciente"}), 500



# CADASTROS INTERNOS OPCIONAIS


@app.route('/patients', methods=['POST'])
def create_patient():
    try:
        if not request.is_json:
            return jsonify({"success": False, "message": "Content-Type deve ser application/json"}), 400

        data = request.get_json()

        full_name = data.get('full_name', '').strip()
        birth_date_str = data.get('birth_date', '').strip()
        gender = data.get('gender', '').strip()
        phone = data.get('phone', '').strip() or None
        email = data.get('email', '').strip() or None
        document_number = data.get('document_number', '').strip() or None
        city = data.get('city', '').strip() or None

        if not full_name:
            return jsonify({"success": False, "message": "Nome do paciente é obrigatório"}), 400

        if not birth_date_str:
            return jsonify({"success": False, "message": "Data de nascimento é obrigatória"}), 400

        if not gender:
            return jsonify({"success": False, "message": "Sexo é obrigatório"}), 400

        birth_date = parse_date(birth_date_str, 'Data de nascimento')

        if email and not validate_email(email):
            return jsonify({"success": False, "message": "Email do paciente inválido"}), 400

        if document_number and Patient.query.filter_by(document_number=document_number).first():
            return jsonify({"success": False, "message": "Documento já cadastrado"}), 409

        patient = Patient(
            full_name=full_name,
            birth_date=birth_date,
            gender=gender,
            phone=phone,
            email=email,
            document_number=document_number,
            city=city
        )

        db.session.add(patient)
        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Paciente cadastrado com sucesso",
            "patient": patient.to_dict()
        }), 201

    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f'Erro ao cadastrar paciente: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao cadastrar paciente"}), 500


@app.route('/appointments', methods=['POST'])
def create_appointment():
    try:
        if not request.is_json:
            return jsonify({"success": False, "message": "Content-Type deve ser application/json"}), 400

        data = request.get_json()

        patient_id = data.get('patient_id')
        professional_id = data.get('professional_id')
        appointment_date_str = data.get('appointment_date', '').strip()
        appointment_type = data.get('appointment_type', '').strip()
        status = data.get('status', 'scheduled').strip()
        notes = data.get('notes', '').strip() or None

        if not patient_id:
            return jsonify({"success": False, "message": "patient_id é obrigatório"}), 400

        if not professional_id:
            return jsonify({"success": False, "message": "professional_id é obrigatório"}), 400

        if not appointment_date_str:
            return jsonify({"success": False, "message": "appointment_date é obrigatório"}), 400

        if not appointment_type:
            return jsonify({"success": False, "message": "appointment_type é obrigatório"}), 400

        patient = Patient.query.get(patient_id)
        if not patient:
            return jsonify({"success": False, "message": "Paciente não encontrado"}), 404

        professional = User.query.get(professional_id)
        if not professional:
            return jsonify({"success": False, "message": "Profissional não encontrado"}), 404

        appointment_date = parse_datetime(appointment_date_str, 'Data da consulta')

        appointment = Appointment(
            patient_id=patient_id,
            professional_id=professional_id,
            appointment_date=appointment_date,
            appointment_type=appointment_type,
            status=status,
            notes=notes
        )

        db.session.add(appointment)
        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Consulta cadastrada com sucesso",
            "appointment": appointment.to_dict()
        }), 201

    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f'Erro ao cadastrar consulta: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao cadastrar consulta"}), 500


@app.route('/exams', methods=['POST'])
def create_exam():
    try:
        if not request.is_json:
            return jsonify({"success": False, "message": "Content-Type deve ser application/json"}), 400

        data = request.get_json()

        patient_id = data.get('patient_id')
        requested_by = data.get('requested_by')
        exam_type = data.get('exam_type', '').strip()
        status = data.get('status', 'pending').strip()
        requested_at_str = data.get('requested_at', '').strip()
        completed_at_str = data.get('completed_at', '').strip()
        result_summary = data.get('result_summary', '').strip() or None

        if not patient_id:
            return jsonify({"success": False, "message": "patient_id é obrigatório"}), 400

        if not requested_by:
            return jsonify({"success": False, "message": "requested_by é obrigatório"}), 400

        if not exam_type:
            return jsonify({"success": False, "message": "exam_type é obrigatório"}), 400

        patient = Patient.query.get(patient_id)
        if not patient:
            return jsonify({"success": False, "message": "Paciente não encontrado"}), 404

        professional = User.query.get(requested_by)
        if not professional:
            return jsonify({"success": False, "message": "Profissional não encontrado"}), 404

        requested_at = datetime.utcnow()
        if requested_at_str:
            requested_at = parse_datetime(requested_at_str, 'Data da solicitação')

        completed_at = None
        if completed_at_str:
            completed_at = parse_datetime(completed_at_str, 'Data de conclusão')

        exam = Exam(
            patient_id=patient_id,
            requested_by=requested_by,
            exam_type=exam_type,
            status=status,
            requested_at=requested_at,
            completed_at=completed_at,
            result_summary=result_summary
        )

        db.session.add(exam)
        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Exame cadastrado com sucesso",
            "exam": exam.to_dict()
        }), 201

    except ValueError as ve:
        return jsonify({"success": False, "message": str(ve)}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f'Erro ao cadastrar exame: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao cadastrar exame"}), 500



# UTILIDADE / DESENVOLVIMENTO

@app.route('/users', methods=['GET'])
def get_users():
    try:
        query = User.query.order_by(User.full_name.asc())
        result = paginate_query(query)
        
        return jsonify({
            "success": True,
            "users": [user.to_dict() for user in result['items']],
            "pagination": {
                "total": result['total'],
                "pages": result['pages'],
                "current_page": result['current_page'],
                "per_page": result['per_page'],
                "has_next": result['has_next'],
                "has_prev": result['has_prev']
            }
        }), 200
    except Exception as e:
        logger.error(f'Erro ao listar usuários: {str(e)}')
        return jsonify({"success": False, "message": "Erro ao buscar usuários"}), 500


@app.route('/health', methods=['GET'])
def health_check():
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({
            "status": "healthy",
            "message": "Servidor funcionando corretamente",
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        logger.error(f'Erro no health check: {str(e)}')
        return jsonify({
            "status": "unhealthy",
            "message": "Erro na conexão com banco de dados",
            "error": str(e)
        }), 503

@app.route('/', methods=['GET'])
def root():
    """Retorna status da API para o Netlify"""
    return jsonify({"status": "API Online", "message": "Backend funcionando. Interface hospedada no Netlify!"}), 200

@app.route('/dashboard/', methods=['GET'])
def dashboard_page():
    """Serve a página principal do dashboard"""
    return send_file(os.path.join(app.static_folder, 'index.html'))

@app.route('/dashboard/index.html', methods=['GET'])
def dashboard_index():
    """Serve o index.html do dashboard"""
    return send_file(os.path.join(app.static_folder, 'index.html'))


@app.errorhandler(404)
def not_found(error):
    return jsonify({"success": False, "message": "Endpoint não encontrado"}), 404

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return jsonify({"success": False, "message": "Erro interno do servidor"}), 500

import zipfile
import os

# Verifica se o CSV NÃO existe, mas o ZIP EXISTE, e extrai o CSV do ZIP
if not os.path.exists('banco_limpo - Copia.csv') and os.path.exists('banco.dat'):
    logger.info("Extraindo a base de dados CSV do arquivo DAT...")
    
    with zipfile.ZipFile('banco.dat', 'r') as zip_ref:
        zip_ref.extractall('.')
        
import time
time.sleep(2) 
import os
logger.info(f"Arquivos na pasta atual: {os.listdir('.')}")
logger.info("Extração concluída.")
    
# 1. Cria as tabelas para o Render (Gunicorn)
with app.app_context():
    create_tables_and_seed()

# 2. Inicia o servidor apenas se rodar localmente
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'

    logger.info(f'Iniciando servidor na porta {port} (debug={debug})')
    app.run(host='0.0.0.0', port=port, debug=debug)


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return jsonify({"success": False, "message": "Erro interno do servidor"}), 500

with app.app_context():
    create_tables_and_seed()

if __name__ == '__main__':
 
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'

    logger.info(f'Iniciando servidor na porta {port} (debug={debug})')
    app.run(host='0.0.0.0', port=port, debug=debug)
    
    