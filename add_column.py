import sqlite3

DB_NAME = 'attendance.db'

def add_student_id_column():
    print(f"Attempting to connect to database: {DB_NAME}")
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # This is the command that fixes the database schema
        cursor.execute("ALTER TABLE attendance ADD COLUMN student_id TEXT")
        
        conn.commit()
        conn.close()
        print(f"✅ SUCCESS: 'student_id' column added to the 'attendance' table.")
        
    except sqlite3.OperationalError as e:
        # This handles cases where the column might already exist (e.g., if you tried running it before)
        if "duplicate column name" in str(e):
            print("⚠️ Warning: Column 'student_id' already exists. Database is likely fixed.")
        else:
            print(f"❌ DATABASE ERROR: Could not modify table. Reason: {e}")
            
    except Exception as e:
        print(f"❌ UNEXPECTED ERROR: {e}")

if __name__ == '__main__':
    add_student_id_column()