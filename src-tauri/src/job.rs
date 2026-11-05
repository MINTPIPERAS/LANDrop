//! Windows job object that kills the sidecar when the shell process dies.
//!
//! A plain `Child::kill` only runs on a clean exit. Assigning the child to a
//! kill-on-close job object covers crashes, forced termination and the debugger
//! stopping the app, so no orphaned Python service is left behind.

#[cfg(windows)]
pub struct JobHandle(windows_sys::Win32::Foundation::HANDLE);

// The handle is only used from the owning thread and the OS object itself is
// thread-safe; the raw pointer just does not implement Send by default.
#[cfg(windows)]
unsafe impl Send for JobHandle {}

#[cfg(windows)]
impl JobHandle {
    /// Create the job and assign `process` to it.
    pub fn attach(process: &std::process::Child) -> Option<JobHandle> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return None;
            }

            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let configured = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *mut _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if configured == 0 {
                return None;
            }

            if AssignProcessToJobObject(job, process.as_raw_handle() as _) == 0 {
                return None;
            }
            Some(JobHandle(job))
        }
    }
}

#[cfg(windows)]
impl Drop for JobHandle {
    fn drop(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;
        // Closing the last handle terminates every process still in the job.
        unsafe {
            CloseHandle(self.0);
        }
    }
}

#[cfg(not(windows))]
pub struct JobHandle;

#[cfg(not(windows))]
impl JobHandle {
    pub fn attach(_process: &std::process::Child) -> Option<JobHandle> {
        None
    }
}
