import sqlite3

DB_NAME = 'attendance.db'

def delete_all_attendance_records():
    print(f"Connecting to database: {DB_NAME}")
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # This SQL command deletes ALL data from the attendance table.
        cursor.execute("DELETE FROM attendance")
        
        # This resets the AUTOINCREMENT counter, ensuring new records start at ID 1.
        cursor.execute("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'attendance'")
        
        conn.commit()
        conn.close()
        print("✅ SUCCESS: All previous records deleted from the 'attendance' table.")
        print("You can now restart your main application.")
        
    except sqlite3.OperationalError as e:
        print(f"❌ DATABASE ERROR: Could not clean up the table. Ensure '{DB_NAME}' is in the correct folder.")
        print(f"Error details: {e}")
            
    except Exception as e:
        print(f"❌ UNEXPECTED ERROR: {e}")

if __name__ == '__main__':
    delete_all_attendance_records()